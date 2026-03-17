"""
CodeGraph Agent - 增强版

支持：
- 递归查询完整调用链
- 意图分类
- 语义搜索
- 动态回答
"""

import os
from typing import TypedDict, Optional, List
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from neo4j import GraphDatabase
from langgraph.graph import StateGraph, END

from embedding import semantic_search, build_embedding_index

# 加载环境变量
load_dotenv()

# --- 1. 配置 Qwen 模型 ---
llm = ChatOpenAI(
    model='qwen3.5-27b',
    openai_api_key=os.getenv("QWEN_API_KEY"),
    openai_api_base=os.getenv("QWEN_BASE_URL"),
    temperature=0.1,
    streaming=True
)


# --- 2. Neo4j 图检索工具 ---

def _get_neo4j_driver():
    """获取 Neo4j 连接"""
    uri = os.getenv("NEO4J_URI")
    auth = (os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
    return GraphDatabase.driver(uri, auth=auth)


def query_dependencies(func_name: str) -> str:
    """查询单层依赖（原有功能）"""
    query = """
    MATCH (target {name: $name})
    OPTIONAL MATCH (target)-[:CALLS]->(downstream)
    OPTIONAL MATCH (upstream)-[:CALLS]->(target)
    RETURN 
        labels(target)[0] as type,
        target.filepath as filepath,
        target.lineno as lineno,
        collect(DISTINCT downstream.name) as calls,
        collect(DISTINCT upstream.name) as called_by
    """
    
    try:
        with _get_neo4j_driver() as driver:
            result = driver.execute_query(query, name=func_name).records
            
            if not result:
                return f"未找到函数 '{func_name}'"
            
            record = result[0]
            return f"""
[目标]: {func_name} ({record['type']})
[位置]: {record['filepath']}:{record['lineno']}
[直接调用]: {record['calls'] or '无'}
[被调用者]: {record['called_by'] or '无'}
"""
    except Exception as e:
        return f"数据库错误: {str(e)}"


def query_recursive_impact(func_name: str, max_depth: int = 5) -> str:
    """递归查询完整影响链（多跳）"""
    
    # 查询所有上游调用者
    upstream_query = f"""
    MATCH (upstream)-[:CALLS*1..{max_depth}]->(target {{name: $name}})
    RETURN distinct upstream.name as name, length(shortestPath((upstream)-[:CALLS*]->(target))) as depth
    ORDER BY depth
    """
    
    # 查询所有下游依赖
    downstream_query = f"""
    MATCH (target {{name: $name}})-[:CALLS*1..{max_depth}]->(downstream)
    RETURN distinct downstream.name as name, length(shortestPath((target)-[:CALLS*]->(downstream))) as depth
    ORDER BY depth
    """
    
    try:
        with _get_neo4j_driver() as driver:
            # 获取基本信息
            basic_info = query_dependencies(func_name)
            
            # 递归查询上游
            upstream_result = driver.execute_query(upstream_query, name=func_name).records
            upstream_list = [f"{r['name']} (深度:{r['depth']})" for r in upstream_result]
            
            # 递归查询下游
            downstream_result = driver.execute_query(downstream_query, name=func_name).records
            downstream_list = [f"{r['name']} (深度:{r['depth']})" for r in downstream_result]
            
            return f"""
{basic_info}

[递归上游影响链] (所有调用它的函数):
{chr(10).join(upstream_list) if upstream_list else '无 - 可能是入口函数'}

[递归下游依赖链] (它调用的所有函数):
{chr(10).join(downstream_list) if downstream_list else '无'}

[影响范围]: 上游 {len(upstream_list)} 个, 下游 {len(downstream_list)} 个
"""
    except Exception as e:
        return f"数据库错误: {str(e)}"


def query_call_chain(start_func: str, end_func: str, max_depth: int = 10) -> str:
    """查询两点之间的调用路径"""
    query = f"""
    MATCH path = shortestPath((start {{name: $start}})-[:CALLS*1..{max_depth}]->(end {{name: $end}}))
    RETURN [n in nodes(path) | n.name] as chain
    """
    
    try:
        with _get_neo4j_driver() as driver:
            result = driver.execute_query(query, start=start_func, end=end_func).records
            
            if not result:
                return f"未找到从 '{start_func}' 到 '{end_func}' 的调用路径"
            
            chain = result[0]['chain']
            return f"调用链: {' -> '.join(chain)}"
    except Exception as e:
        return f"数据库错误: {str(e)}"


def get_call_depths() -> dict:
    """获取所有节点的调用深度（用于层级可视化）"""
    query = """
    MATCH (n)
    WHERE NOT (n)<-[:CALLS]-()
    CALL {
        WITH n
        MATCH path = (n)-[:CALLS*]->(m)
        RETURN max(length(path)) as depth
    }
    RETURN n.name as name, coalesce(depth, 0) as depth
    """
    
    try:
        with _get_neo4j_driver() as driver:
            result = driver.execute_query(query).records
            return {r['name']: r['depth'] for r in result}
    except Exception as e:
        print(f"获取深度失败: {e}")
        return {}


# --- 3. 意图分类 ---

INTENT_PROMPT = """
分析用户问题，判断意图类型：

用户问题: {query}

意图类型：
1. impact_analysis - 影响分析（修改某函数会影响谁）
2. call_chain - 调用链查询（A到B的调用路径）
3. semantic_search - 语义搜索（查找某功能的函数）
4. function_info - 函数信息（了解某函数的功能）

只返回 JSON 格式：
{{"intent": "类型", "target": "目标函数名", "target2": "第二个函数名(如果是call_chain)"}}
"""


def classify_intent(query: str) -> dict:
    """使用 LLM 分类用户意图"""
    response = llm.invoke(INTENT_PROMPT.format(query=query))
    
    try:
        import json
        # 尝试解析 JSON
        content = response.content
        # 提取 JSON 部分
        if '{' in content and '}' in content:
            json_str = content[content.find('{'):content.rfind('}')+1]
            return json.loads(json_str)
    except:
        pass
    
    # 默认返回
    return {"intent": "impact_analysis", "target": ""}


# --- 4. 定义 Agent 状态 ---
class AgentState(TypedDict):
    query: str
    intent: str
    target_func: str
    target_func2: Optional[str]
    context: str
    response: str
    revision_count: int
    feedback: str
    entities: Optional[List[dict]]


# --- 5. 节点函数 ---

def parse_intent(state: AgentState):
    """节点 1: 意图识别"""
    print("--- [Step 1] 解析意图 ---")
    
    # 使用 LLM 分类
    intent_result = classify_intent(state['query'])
    
    print(f"--- 意图: {intent_result['intent']}, 目标: {intent_result.get('target', '')} ---")
    
    return {
        "intent": intent_result.get("intent", "impact_analysis"),
        "target_func": intent_result.get("target", ""),
        "target_func2": intent_result.get("target2"),
        "revision_count": 0
    }


def retrieve_graph(state: AgentState):
    """节点 2: 图谱检索（根据意图选择不同查询）"""
    print(f"--- [Step 2] 检索图谱 ---")
    
    intent = state['intent']
    target = state['target_func']
    
    if intent == "semantic_search":
        # 语义搜索
        results = semantic_search(state['query'], top_k=5)
        if results:
            context = "语义搜索结果:\n"
            for r in results:
                context += f"\n- {r['name']} (相似度: {r['similarity']:.2f})\n"
                context += f"  文件: {r['filepath']}:{r['lineno']}\n"
                if r['docstring']:
                    context += f"  说明: {r['docstring'][:100]}\n"
        else:
            context = "未找到相关函数"
    
    elif intent == "call_chain" and state.get('target_func2'):
        # 调用链查询
        context = query_call_chain(target, state['target_func2'])
    
    elif intent == "impact_analysis":
        # 递归影响分析
        context = query_recursive_impact(target)
    
    else:
        # 默认：函数信息
        context = query_dependencies(target)
    
    return {"context": context}


def generate_answer(state: AgentState):
    """节点 3: 动态生成回答"""
    print("--- [Step 3] 生成回答 ---")
    
    intent = state['intent']
    
    # 根据意图使用不同的 prompt
    if intent == "semantic_search":
        prompt = f"""
你是一个代码助手。用户问题: {state['query']}

搜索结果:
{state['context']}

请用中文直接回答用户的问题，推荐最相关的函数并简要说明用途。保持简洁。
"""
    elif intent == "call_chain":
        prompt = f"""
你是一个代码助手。用户问题: {state['query']}

调用链分析:
{state['context']}

请用中文解释这个调用链的含义，说明数据是如何从起点流向终点的。保持简洁。
"""
    elif intent == "impact_analysis":
        prompt = f"""
你是一个资深 Python 架构师。用户问题: {state['query']}

影响分析结果:
{state['context']}

请用中文分析修改该函数的影响，指出风险等级（低/中/高），并给出具体建议。保持简洁专业。
"""
    else:
        prompt = f"""
你是一个代码助手。用户问题: {state['query']}

上下文:
{state['context']}

请用中文直接回答用户的问题。保持简洁。
"""
    
    response = llm.invoke(prompt)
    return {"response": response.content}


def review_answer(state: AgentState):
    """节点 4: 结果审查"""
    print("--- [Step 4] 审查回答 ---")
    
    prompt = f"""
审查员检查回答质量。

问题: {state['query']}
上下文: {state['context']}
回答: {state['response']}

检查:
1. 回答是否与上下文一致（没有编造信息）？
2. 回答是否使用中文？

正常输出 "PASS"，有问题输出 "FAIL: 原因"
"""
    
    review = llm.invoke(prompt).content
    print(f"--- 审查结果: {review[:50]}... ---")
    
    if "PASS" in review:
        return {"feedback": "PASS"}
    else:
        return {"feedback": review, "revision_count": state["revision_count"] + 1}


# --- 6. 路由逻辑 ---

def check_review_outcome(state: AgentState):
    if state["feedback"] == "PASS" or state["revision_count"] >= 1:
        return "end"
    else:
        return "retry"


# --- 7. 构建工作流 ---

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
