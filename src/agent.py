import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from neo4j import GraphDatabase
from langgraph.graph import StateGraph, END
from typing import TypedDict

load_dotenv()

# --- 1. 初始化 Qwen 模型 ---
# 确保你的 .env 文件里有 QWEN_API_KEY 和 QWEN_BASE_URL
llm = ChatOpenAI(
    model='qwen-plus', # 或者 'qwen-2.5-coder-32b-instruct'
    openai_api_key=os.getenv("QWEN_API_KEY"),
    openai_api_base=os.getenv("QWEN_BASE_URL"),
    temperature=0.1
)

# --- 2. Neo4j 查询工具 (核心修复部分) ---
def query_dependencies(func_name: str):
    uri = os.getenv("NEO4J_URI")
    auth = (os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
    
    # 双向查询：查下游(我调了谁) + 上游(谁调了我)
    query = """
    MATCH (target:Function {name: $name})
    
    // 1. 下游依赖 (Outbound): 我调用了谁
    OPTIONAL MATCH (target)-[:CALLS]->(downstream)
    
    // 2. 上游影响 (Inbound): 谁调用了我
    OPTIONAL MATCH (upstream)-[:CALLS]->(target)
    
    RETURN 
        target.filepath as filepath, 
        target.lineno as lineno,
        collect(DISTINCT downstream.name) as calls,
        collect(DISTINCT upstream.name) as called_by
    """
    
    try:
        with GraphDatabase.driver(uri, auth=auth) as driver:
            # 执行查询
            result = driver.execute_query(query, name=func_name).records
            
            # 如果没查到
            if not result:
                return f"Function '{func_name}' not found in Knowledge Graph. Please check the function name."
            
            # 获取第一条记录
            record = result[0]
            
            # 格式化返回结果给 LLM
            return f"""
            [Target Function]: {func_name}
            [Location]: {record['filepath']} (Line: {record.get('lineno', 'N/A')})
            
            [Dependency Graph Analysis]
            1. Outbound Calls (Dependencies):
               {record['calls']}
               -> Modifying '{func_name}' might change the behavior passed to these functions.
               
            2. Inbound Calls (Called By):
               {record['called_by']}
               -> WARNING: Modifying '{func_name}' will directly affect these upstream functions.
            """
            
    except Exception as e:
        return f"Database Error: {str(e)}"

# --- 3. LangGraph 状态与节点定义 ---

class AgentState(TypedDict):
    query: str
    target_func: str
    context: str
    response: str

def parse_intent(state: AgentState):
    # 简单的提取逻辑：取最后一个单词作为函数名
    # 实际项目中可以使用 LLM 进行提取
    txt = state['query']
    # 移除常见标点
    for char in ".,?!'\"":
        txt = txt.replace(char, "")
    
    words = txt.split()
    target = words[-1] if words else ""
    return {"target_func": target}

def retrieve_graph(state: AgentState):
    # 调用上面的 Neo4j 查询函数
    context = query_dependencies(state['target_func'])
    return {"context": context}

def generate_answer(state: AgentState):
    prompt = f"""
    你是一位高级Python架构师和代码重构助手, 并用中文回答。
    
    User Query: "{state['query']}"
    
    === Knowledge Graph Context ===
    {state['context']}
    ===============================
    
    Task: 
    1. Analyze the impact of modifying the function '{state['target_func']}'.
    2. Specifically mention which functions will be broken (upstream callers).
    3. Provide a brief refactoring risk assessment (Low/Medium/High).
    """
    
    # 调用 Qwen
    msg = llm.invoke(prompt)
    return {"response": msg.content}

# --- 4. 构建工作流图 ---
workflow = StateGraph(AgentState)

# 添加节点
workflow.add_node("parse", parse_intent)
workflow.add_node("retrieve", retrieve_graph)
workflow.add_node("generate", generate_answer)

# 定义连线
workflow.set_entry_point("parse")
workflow.add_edge("parse", "retrieve")
workflow.add_edge("retrieve", "generate")
workflow.add_edge("generate", END)

# 编译应用
app_graph = workflow.compile()