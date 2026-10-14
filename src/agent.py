import os
import operator
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
def query_dependencies(func_name: str) -> str:
    """
    在 Neo4j 中执行双向查询
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

            if len(result) > 1:
                matches = ", ".join(r["uid"] for r in result)
                return (f"⚠️ 函数名 '{func_name}' 在图中匹配到多个实体: {matches}。"
                        f"请使用 module:name 形式指定唯一目标。")

            if not result:
                return f"⚠️ 警告：在知识图谱中未找到函数 '{func_name}'。请检查函数名是否正确。"
            
            record = result[0]
            display = record["uid"] or func_name
            
            # 这里保留英文 Key 是为了让 LLM 更准确地理解结构，但我们在 Prompt 里会要求它用中文解释
            context_str = f"""
            [目标实体]: {display}
            [模块]: {record['module'] or '未知'}
            [位置]: {record['filepath']} (行号: {record.get('lineno', '未知')})
            
            [图谱依赖分析]
            1. 下游依赖 (它调用了谁 / Outbound):
               {record['calls'] if record['calls'] else "无 (None)"}
               -> 修改 {display} 可能会改变传给这些函数的参数。
               
            2. 上游影响 (谁调用了它 / Inbound):
               {record['called_by'] if record['called_by'] else "无 (None - 可能是入口函数或未被使用)"}
               -> 严重警告: 修改 {display} 将直接导致这些调用者出错。
            """
            return context_str
            
    except Exception as e:
        return f"数据库连接错误: {str(e)}"

# --- 3. 定义 Agent 状态 ---
class AgentState(TypedDict):
    query: str
    target_func: str
    context: str
    response: str
    revision_count: int
    feedback: str

# --- 4. 节点函数定义 ---

def parse_intent(state: AgentState):
    """节点 1: 意图识别"""
    print("--- [Step 1] 解析意图 ---")
    txt = state['query']
    clean_txt = txt.strip().rstrip("?.!")
    words = clean_txt.split()
    target = words[-1] if words else ""
    # 如果用户输入包含中文标点，可能需要额外处理，这里简单处理
    target = target.replace("。", "").replace("？", "")
    return {"target_func": target, "revision_count": 0}

def retrieve_graph(state: AgentState):
    """节点 2: 图谱检索"""
    print(f"--- [Step 2] 检索图谱: {state['target_func']} ---")
    context = query_dependencies(state['target_func'])
    return {"context": context}

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
    return {"response": response.content}

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
        return {"feedback": "PASS"}
    else:
        return {"feedback": review, "revision_count": state["revision_count"] + 1}

# --- 5. 路由逻辑 ---

def check_review_outcome(state: AgentState):
    if state["feedback"] == "PASS" or state["revision_count"] >= 1:
        return "end"
    else:
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
        "retry": "generate"
    }
)
app_graph = workflow.compile()
