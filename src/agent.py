import os
import operator
import re
from typing import TypedDict, Annotated, List, Union
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, BaseMessage
from neo4j import GraphDatabase
from langgraph.graph import StateGraph, END

# 加载环境变量
load_dotenv()

# --- 1. 配置 Qwen 模型 (惰性构造) ---
# 惰性构造使本模块在无 API Key 时也能被导入, 从而让图查询逻辑可离线测试。
_llm = None


def get_llm():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(
            model=os.getenv("QWEN_MODEL", "qwen3.5-27b"),
            openai_api_key=os.getenv("QWEN_API_KEY"),
            openai_api_base=os.getenv("QWEN_BASE_URL"),
            temperature=0.1,
            streaming=True,
        )
    return _llm

# --- 2. Neo4j 图检索工具 (GraphRAG 核心) ---
def query_dependencies(func_name: str) -> dict:
    """在 Neo4j 中执行双向查询, 返回结构化记录。

    返回 dict 而不是渲染好的字符串: 可观测性需要从中取出 calls/called_by 做
    集合运算。从渲染文本反解析会把 module 名 (auth.service)、filepath
    (service.py)、中文小标题一并吃进 retrieved 集合, 使幻觉率虚低、覆盖率
    虚高。渲染交给 render_context()。

    记录形状:
        {'uid', 'name', 'module', 'filepath', 'lineno', 'calls', 'called_by'}
    失败时返回 {'error': str} 且无 uid —— 调用方据此区分"检索失败"与
    "模型在编造", 前者不得计入幻觉率。
    """
    uri = os.getenv("NEO4J_URI")
    auth = (os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))

    # 限定 label 到 Function/Method/Class: 旧写法 MATCH (target:Function) 无法匹配方法,
    # 而放宽到不带 label 又会让同名 Class/Function 互相串。
    query = """
    MATCH (target)
    WHERE (target:Function OR target:Method OR target:Class)
      AND (target.name = $name OR target.uid ENDS WITH $suffix)
    OPTIONAL MATCH (target)-[:CALLS]->(downstream)
    OPTIONAL MATCH (upstream)-[:CALLS]->(target)
    RETURN
        target.uid as uid,
        target.name as name,
        target.module as module,
        target.filepath as filepath,
        target.lineno as lineno,
        collect(DISTINCT downstream.name) as calls,
        collect(DISTINCT upstream.name) as called_by
    """
    suffix = f":{func_name}"

    try:
        with GraphDatabase.driver(uri, auth=auth) as driver:
            result = driver.execute_query(query, name=func_name, suffix=suffix).records
    except Exception as e:
        return {"error": f"数据库连接错误: {e}"}

    if len(result) > 1:
        matches = ", ".join(r["uid"] for r in result)
        return {"error": f"⚠️ 函数名 '{func_name}' 在图中匹配到多个实体: {matches}。"
                         f"请使用 module:name 形式指定唯一目标。"}

    if not result:
        return {"error": f"⚠️ 警告：在知识图谱中未找到函数 '{func_name}'。请检查函数名是否正确。"}

    record = result[0]
    # collect() 在无匹配时返回 [None], 过滤掉以免污染 retrieved 集合
    calls = [c for c in (record["calls"] or []) if c]
    called_by = [c for c in (record["called_by"] or []) if c]

    return {
        "uid": record["uid"] or func_name,
        "name": record["name"],
        "module": record["module"],
        "filepath": record["filepath"],
        "lineno": record.get("lineno"),
        "calls": calls,
        "called_by": called_by,
    }


def render_context(context: dict) -> str:
    """把结构化上下文渲染成给 LLM 的文本。

    Key 保留英文是为了让 LLM 更准确地理解结构, Prompt 里再要求它用中文解释。
    """
    if context.get("error"):
        return context["error"]

    display = context["uid"]
    calls = context["calls"] or ["无 (None)"]
    called_by = context["called_by"] or ["无 (None - 可能是入口函数或未被使用)"]

    return f"""
            [目标实体]: {display}
            [模块]: {context['module'] or '未知'}
            [位置]: {context['filepath']} (行号: {context['lineno'] or '未知'})

            [图谱依赖分析]
            1. 下游依赖 (它调用了谁 / Outbound):
               {calls}
               -> 修改 {display} 可能会改变传给这些函数的参数。

            2. 上游影响 (谁调用了它 / Inbound):
               {called_by}
               -> 严重警告: 修改 {display} 将直接导致这些调用者出错。
            """

# --- 3. 定义 Agent 状态 ---
class AgentState(TypedDict):
    query: str
    target_func: str
    context: str              # 渲染后的图谱上下文, 直接进 Prompt
    graph_record: dict         # 结构化图谱事实 (calls / called_by / uid)
    response: str
    revision_count: int
    feedback: str
    # 轨迹采集字段 (observability)
    nodes_visited: List[str]
    review_verdicts: List[str]
    retrieved_symbols: List[str]
    terminated_by: str
    retrieval_failed: bool


# --- 3.1 轨迹采集 (见 docs/specs/SPEC-observability.md) ---
# 审查未通过时允许的重试次数。与 review_answer / check_review_outcome 共用,
# 避免两处各写一个字面量导致口径不一致。
MAX_REVISIONS = 1

# 工作流是线性流, 用可变 state 累积节点访问与审查结论; 路由函数读它决定
# terminated_by。不引入全局变量, 以免并发 invoke 时串味。
def _visited(state, node):
    return list(state.get("nodes_visited") or []) + [node]


def _retrieved_symbols(context):
    """从图谱上下文里取出检索到的符号名 (结构化字段, 不做文本解析)。

    query_dependencies 返回的上下文形如:
        {'uid':..., 'calls': [...], 'called_by': [...]}
    用 record['calls'] 而不是从渲染后的字符串里正则抽取 —— 上下文里还含
    module 名 (auth.service)、filepath (service.py)、中文小标题, 正则会把
    它们一并吃进 retrieved, 导致覆盖率虚高、幻觉率虚低。
    """
    context = context or {}
    if not isinstance(context, dict):
        return set()

    # 目标自身必须计入: 回答里"修改 `validate_token` 会影响 ..." 是对目标的
    # 事实陈述, 不计入就会把最确定的那个符号判成幻觉, 使幻觉率虚高。
    names = [context.get("name"), context.get("uid")]
    names += list(context.get("calls") or []) + list(context.get("called_by") or [])

    out = set()
    for n in filter(None, names):
        # 同一符号在回答里可能写成裸名 / module:qualname / Class.method,
        # 三种写法都算命中, 否则幻觉率会随 LLM 的书写习惯漂移。
        out.add(n)
        out.add(n.split(":", 1)[1] if ":" in n else n)
        out.add(n.rsplit(".", 1)[1] if "." in n else n)
    return {n for n in out if n not in _NON_SYMBOL_WORDS}


def _retrieval_failed(context):
    """检索是否失败 (库不可达 / 未找到目标)。

    这个区分对指标至关重要: 检索失败时 retrieved 为空, 若不标记,
    幻觉率会被算成 1.0, 把"连不上库"误报成"模型在编造"。
    """
    if not context:
        return True
    if isinstance(context, str):
        # 兼容旧调用方直接传渲染文本
        return any(marker in context for marker in _RETRIEVAL_ERROR_MARKERS)
    # 结构化上下文中没有 uid 即表示未命中 (未找到 / 多个匹配 / 连接失败)
    return not context.get("uid")


_RETRIEVAL_ERROR_MARKERS = ("数据库连接错误", "未找到函数", "在知识图谱中未找到")


# 上下文模板里的提示词, 不是代码符号
_NON_SYMBOL_WORDS = {"None", "target", "filepath", "lineno", "Outbound", "Inbound"}

# --- 4. 节点函数定义 ---

def extract_target_symbol(query):
    """从自然语言问题中抽取目标符号名。

    优先级:
    1. 反引号片段 (用户显式标注, 最可信)
    2. 点分限定名 (auth.service:AuthService.login / AuthService.login)
    3. snake_case / CamelCase 标识符
    4. 任意长度 >= 3 的标识符 (裸小写函数名 login / save)

    第 4 级是必需的: 硬伤 #1 的原始修法只认 snake_case/CamelCase, 于是
    "修改 login 会影响谁" 抽不出任何符号 —— 普通小写函数名在真实代码里
    占比很高, 过滤过头等于没修。代价是会命中普通英文词, 由
    resolve_target_symbol() 用图校验兜底。

    找不到任何标识符时返回空串 —— 由调用方转为 "未找到" 提示,
    而不是像旧实现那样硬拿最后一个词。
    """
    if not query:
        return ""

    # 1. 反引号优先
    for candidate in re.findall(r"`([^`]+)`", query):
        candidate = candidate.strip()
        if _looks_like_symbol(candidate):
            return candidate

    # 2. 点分限定名
    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", query):
        if _looks_like_symbol(match.group(0)):
            return match.group(0)

    # 3. snake_case / CamelCase, 取最长 —— 通常信息量最大
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query)
    shaped = [w for w in words if _looks_like_symbol(w)]
    if shaped:
        return max(shaped, key=len)

    # 4. 裸小写标识符兜底; 长度 >= 3 以避开 "of" / "to" / "a" 这类虚词
    long_words = [w for w in words if len(w) >= 3]
    return max(long_words, key=len) if long_words else ""


def resolve_target_symbol(query):
    """带图校验的符号解析: 逐候选查询, 返回 (目标, 检索记录)。

    为什么需要这一步: extract_target_symbol 的第 4 级会命中普通英文词
    ("modify" / "function")。此时若直接把候选当目标, 检索必然落空。
    候选按信息量降序排列, 因此"第一个命中"通常是用户真正想问的符号。

    返回记录而非重新查询: parse 阶段已经付过一次 Cypher 成本, 让
    retrieve 节点复用它, 避免同一个目标被查两遍。
    """
    candidates = target_candidates(query)
    if not candidates:
        return "", {"error": "⚠️ 未能从问题中识别出目标符号, 请用反引号标注, 例如 `login`。"}

    last = None
    for candidate in candidates:
        record = query_dependencies(candidate)
        last = record
        # "匹配到多个实体" 不算命中: 目标不唯一, 答案会有歧义
        if not record.get("error"):
            return candidate, record

    return candidates[0], last


def target_candidates(query):
    """按信息量降序返回全部候选符号 (含裸小写标识符)。"""
    if not query:
        return []

    candidates = []

    # 反引号与点分限定名优先, 且不参与后续去重比较
    for match in re.findall(r"`([^`]+)`", query):
        token = match.strip()
        if _looks_like_symbol(token):
            candidates.append(token)

    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query)
    for token in words:
        if _looks_like_symbol(token):
            candidates.append(token)
    # 裸小写标识符排在 snake_case/CamelCase 之后
    candidates.extend(w for w in words if len(w) >= 3 and w not in candidates)

    # 去重保序
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def _looks_like_symbol(token):
    """判断 token 是否可能是代码符号, 过滤普通英文/中文词。"""
    if not token or len(token) < 2:
        return False
    if "_" in token or "." in token:
        return True
    # CamelCase: 首字母大写且含小写, 或非首部含大写
    if token[0].isupper() and any(c.islower() for c in token):
        return True
    if any(c.isupper() for c in token[1:]):
        return True
    return False


def parse_intent(state: AgentState):
    """节点 1: 意图识别"""
    print("--- [Step 1] 解析意图 ---")
    # 旧实现取 query 最后一个词 (words[-1]): "修改 process_data 会影响谁"
    # 会被解析成 "会影响谁", 检索必然落空。改为从问题中提取标识符。
    #
    # cartog 在 ContextBench 上踩过同一个坑: 把 issue 原文当查询时, 散文词
    # 匹配到 markdown 符号 -> 0 结果; 抽取标识符后 0 -> 6+ 结果。
    # parse 阶段即完成图校验: 候选按信息量降序, 第一个命中就是目标。
    # 记录随 state 传给 retrieve, 避免同一目标被查两遍。
    target, record = resolve_target_symbol(state['query'])
    return {
        "target_func": target,
        "graph_record": record,
        "revision_count": 0,
        "nodes_visited": _visited(state, "parse"),
        "review_verdicts": [],
    }

def retrieve_graph(state: AgentState):
    """节点 2: 图谱检索"""
    print(f"--- [Step 2] 检索图谱: {state['target_func']} ---")
    record = state.get("graph_record") or query_dependencies(state['target_func'])
    return {
        # context 是给 LLM 的渲染文本; 结构化事实另存, 便于 UI / 埋点消费
        "context": render_context(record),
        "graph_record": record,
        "nodes_visited": _visited(state, "retrieve"),
        "retrieval_failed": _retrieval_failed(record),
        "retrieved_symbols": sorted(_retrieved_symbols(record)),
    }

def generate_answer(state: AgentState):
    """节点 3: 生成回答 (中文版)"""
    print("--- [Step 3] 生成回答 ---")
    
    # 🔥🔥🔥 核心修改：将 Prompt 改为中文 🔥🔥🔥
    prompt = f"""
    你是一位资深的 Python 架构师和代码重构专家。
    
    用户问题: "{state['query']}"
    
    === 代码知识图谱上下文 ===
    {state['context']}
    ===============================
    
    之前的审查反馈 (如果有): {state.get('feedback', '无')}
    
    任务:
    请基于图谱上下文，分析修改函数 '{state['target_func']}' 带来的影响。
    
    要求:
    1. **必须使用中文回答**。
    2. 明确指出风险等级 (低/中/高)。
    3. 分别说明对“下游依赖”和“上游调用者”的影响。
    4. 如果上下文中显示“无”，请明确说明该函数可能是一个孤立函数或入口点。
    5. 保持专业、条理清晰，使用 Markdown 格式。
    """
    
    response = get_llm().invoke(prompt)
    return {
        "response": response.content,
        "nodes_visited": _visited(state, "generate"),
    }

def review_answer(state: AgentState):
    """节点 4: 结果审查 (中文版)"""
    print("--- [Step 4] 审查回答 ---")
    
    prompt = f"""
    你是一个代码助手 QA 审查员。
    
    图谱上下文: {state['context']}
    生成的回答: {state['response']}
    
    请检查:
    1. 回答是否遗漏了上下文中的关键影响（特别是“上游影响/Called By”）？
    2. 回答是否产生了幻觉（编造了不存在的依赖）？
    3. **回答是否使用了中文？**
    
    如果一切正常，仅输出 "PASS"。
    如果有问题，输出 "FAIL: <具体原因>"。
    """
    
    review = get_llm().invoke(prompt).content
    print(f"--- 审查结果: {review} ---")
    
    if "PASS" in review:
        return {
            "feedback": "PASS",
            "terminated_by": "pass",
            "nodes_visited": _visited(state, "review"),
        }
    else:
        # revision_count 是"已经用掉的重试次数"。自增前判断, 否则第一次 FAIL
        # 就会把计数推到 1, 路由器随即认为次数用尽 -> 永远重试不了。
        exhausted = state.get("revision_count", 0) >= MAX_REVISIONS
        return {
            "feedback": review,
            "revision_count": state["revision_count"] + 1,
            "terminated_by": "max_revision" if exhausted else "retry",
            "nodes_visited": _visited(state, "review"),
            "review_verdicts": list(state.get("review_verdicts") or []) + [review],
        }

# --- 5. 路由逻辑 ---

def check_review_outcome(state: AgentState):
    """路由: PASS 结束; 否则重试, 但最多 1 次。

    注意: 这里同时决定 terminated_by 的口径。旧实现里 "审查通过" 与
    "重试次数用尽被迫放行" 在最终输出上无法区分, 导致 "自修正有效"
    这个说法无法验证。现在两者分别记为 pass / max_revision。

     LangGraph 约束 (实测两条, 均会导致静默丢数据):
     1. 路由必须返回**分支键字符串**, 返回 dict 会报
        `TypeError: unhashable type: 'dict'`;
     2. 路由里就地修改 state 不会进入最终 state。
     因此 terminated_by 由 review 节点写入, 路由只负责选边。
    """
    if state["feedback"] == "PASS":
        return "end"
    if state["revision_count"] > MAX_REVISIONS:
        return "end"
    return "retry"

# --- 6. 构建工作流 ---

workflow = StateGraph(AgentState)

workflow.add_node("parse", parse_intent)
workflow.add_node("retrieve", retrieve_graph)
workflow.add_node("generate", generate_answer)
workflow.add_node("review", review_answer)

workflow.set_entry_point("parse")
workflow.add_edge("parse", "retrieve")
workflow.add_edge("retrieve", "generate")
workflow.add_edge("generate", "review")

workflow.add_conditional_edges(
    "review",
    check_review_outcome,
    {
        "end": END,
        "retry": "generate",
    }
)
app_graph = workflow.compile()


def run_analysis(query: str, trace_path=None):
    """跑一次分析并记录轨迹。

    返回 (result_state, trace_record)。trace_record 为 None 表示埋点已禁用
    (CODEGRAPH_TRACE=0)。
    """
    from observability.trace import TraceTimer, record_trace

    with TraceTimer() as timer:
        result = app_graph.invoke({"query": query})

    trace = record_trace(
        trace_path,
        query=query,
        target_func=result.get("target_func"),
        nodes_visited=result.get("nodes_visited", []),
        revision_count=result.get("revision_count", 0),
        review_verdicts=result.get("review_verdicts", []),
        retrieved_symbols=set(result.get("retrieved_symbols") or []),
        retrieval_failed=bool(result.get("retrieval_failed")),
        response=result.get("response", ""),
        terminated_by=result.get("terminated_by", "unknown"),
        duration_ms=timer.ms,
    )
    return result, trace
