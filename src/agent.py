import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from neo4j import GraphDatabase
from langgraph.graph import StateGraph, END
from typing import TypedDict

load_dotenv()

# 初始化 Qwen
llm = ChatOpenAI(
    model='qwen-plus',
    openai_api_key=os.getenv("QWEN_API_KEY"),
    openai_api_base=os.getenv("QWEN_BASE_URL"),
    temperature=0
)

# Neo4j 查询工具
def query_dependencies(func_name: str):
    uri = os.getenv("NEO4J_URI")
    auth = (os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
    
    query = """
    MATCH (f:Function {name: $name})
    OPTIONAL MATCH (f)-[:CALLS*1..2]->(dep)
    RETURN f.filepath, f.lineno, collect(distinct dep.name) as dependencies
    """
    
    with GraphDatabase.driver(uri, auth=auth) as driver:
        result = driver.execute_query(query, name=func_name).records
        if not result:
            return f"Function '{func_name}' not found in Knowledge Graph."
        
        record = result[0]
        deps = record["dependencies"]
        return f"Function: {func_name}\nFile: {record['f.filepath']}:{record['f.lineno']}\nDirect & Indirect Calls: {deps}"

# ------ LangGraph 定义 ------

class AgentState(TypedDict):
    query: str
    target_func: str
    context: str
    response: str

def parse_intent(state: AgentState):
    # 简化版意图识别：直接假设用户最后提到了函数名
    # 实际可用 LLM 提取
    words = state['query'].split()
    target = words[-1] if words else ""
    # 去除可能标点
    target = target.strip(".,?!'")
    return {"target_func": target}

def retrieve_graph(state: AgentState):
    context = query_dependencies(state['target_func'])
    return {"context": context}

def generate_answer(state: AgentState):
    prompt = f"""
    You are a Senior Code Refactoring Assistant.
    
    User Query: {state['query']}
    
    Code Knowledge Graph Context:
    {state['context']}
    
    Task: Analyze the impact of modifying the function '{state['target_func']}'.
    Explain which other functions might be affected based on the dependency chain.
    """
    msg = llm.invoke(prompt)
    return {"response": msg.content}

# 构建图
workflow = StateGraph(AgentState)
workflow.add_node("parse", parse_intent)
workflow.add_node("retrieve", retrieve_graph)
workflow.add_node("generate", generate_answer)

workflow.set_entry_point("parse")
workflow.add_edge("parse", "retrieve")
workflow.add_edge("retrieve", "generate")
workflow.add_edge("generate", END)

app_graph = workflow.compile()