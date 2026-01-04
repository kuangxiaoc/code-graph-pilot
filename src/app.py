import streamlit as st
import tempfile
import os
from dotenv import load_dotenv
from neo4j import GraphDatabase

# 引入可视化组件
from streamlit_agraph import agraph, Node, Edge, Config

# 引入自定义模块 (确保这些文件在同级目录或 pythonpath 下)
from graph_builder import build_graph_from_file
from agent import app_graph

# 加载环境变量 (.env)
load_dotenv()

# --- 页面配置 ---
st.set_page_config(
    page_title="CodeGraph Pilot",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 工具函数：获取 Neo4j 数据用于可视化 ---
def get_graph_data(limit=100):
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    pwd = os.getenv("NEO4J_PASSWORD")
    
    nodes = []
    edges = []
    node_ids = set()
    
    query = f"""
    MATCH (n)-[r]->(m)
    RETURN n, r, m
    LIMIT {limit}
    """
    
    try:
        with GraphDatabase.driver(uri, auth=(user, pwd)) as driver:
            results = driver.execute_query(query).records
            
            for record in results:
                src = record['n']
                dst = record['m']
                rel = record['r']
                
                # 处理源节点
                if src['name'] not in node_ids:
                    nodes.append(Node(
                        id=src['name'], 
                        label=src['name'], 
                        size=25, 
                        shape="dot",
                        color="#FF6B6B", # 红色代表源/Function
                        title=f"File: {src.get('filepath', 'N/A')}" # 鼠标悬停显示信息
                    ))
                    node_ids.add(src['name'])
                
                # 处理目标节点
                if dst['name'] not in node_ids:
                    nodes.append(Node(
                        id=dst['name'], 
                        label=dst['name'], 
                        size=20, 
                        shape="dot",
                        color="#4D96FF" # 蓝色代表被调用方
                    ))
                    node_ids.add(dst['name'])
                
                # 处理边
                edges.append(Edge(
                    source=src['name'], 
                    target=dst['name'], 
                    label=rel.type, # 显示 "CALLS"
                    color="#A0A0A0"
                ))
    except Exception as e:
        st.error(f"无法连接数据库获取图谱数据: {e}")
        return [], []
            
    return nodes, edges

# --- 侧边栏：项目设置与建图 ---
with st.sidebar:
    st.title("🛠️ Project Controls")
    st.markdown("---")
    
    st.header("1. Code Ingestion")
    uploaded_file = st.file_uploader("Upload Python File (.py)", type=["py"])
    
    if uploaded_file and st.button("🚀 Build Knowledge Graph", type="primary"):
        with st.spinner("Parsing AST & Building Graph in Neo4j..."):
            # 保存临时文件用于解析
            with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as tmp:
                tmp.write(uploaded_file.getvalue())
                tmp_path = tmp.name
            
            try:
                # 调用 graph_builder
                nodes_count, rels_count = build_graph_from_file(
                    tmp_path, 
                    os.getenv("NEO4J_URI"),
                    os.getenv("NEO4J_USER"),
                    os.getenv("NEO4J_PASSWORD")
                )
                st.success(f"✅ Success! Graph Updated.")
                st.metric("Nodes", nodes_count)
                st.metric("Relations", rels_count)
            except Exception as e:
                st.error(f"Build Failed: {e}")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
    
    st.markdown("---")
    st.info("💡 Powered by **Qwen-2.5** & **Neo4j**")

# --- 主界面 ---
st.title("CodeGraph Pilot ✈️")
st.subheader("Structure-Aware Code Intelligence Assistant")

# 使用 Tabs 分离功能
tab1, tab2 = st.tabs(["💬 Chat & Analysis", "🕸️ Graph Visualization"])

# === Tab 1: 对话界面 (RAG + Qwen) ===
with tab1:
    st.markdown("#### Ask about dependency impact, refactoring risks, or code logic.")
    
    # 示例问题生成
    example_q = "Analyze the impact of modifying function 'process_data'"
    query = st.text_area("Your Question:", value=example_q, height=100)
    
    col_act1, col_act2 = st.columns([1, 5])
    with col_act1:
        run_btn = st.button("🤖 Run Agent")
    
    if run_btn:
        if not query:
            st.warning("Please enter a question.")
        else:
            with st.spinner("Agent is thinking (Retrieving Graph + Qwen Reasoning)..."):
                try:
                    # 调用 Agent (LangGraph)
                    result = app_graph.invoke({"query": query})
                    
                    # 1. 显示检索到的图上下文 (GraphRAG证据)
                    with st.expander("🔍 Knowledge Graph Context (Evidence)", expanded=False):
                        st.json(result.get("context", "No context found"))
                        # 或者如果 context 是字符串，用 st.code(result["context"])
                    
                    # 2. 显示 LLM 回答
                    st.markdown("### 💡 Qwen's Analysis")
                    st.markdown(result["response"])
                    
                except Exception as e:
                    st.error(f"Agent Execution Failed: {e}")

# === Tab 2: 图谱可视化 ===
with tab2:
    st.markdown("#### Interactive Code Dependency Graph")
    st.caption("Scroll to zoom, drag to move nodes.")
    
    if st.button("🔄 Refresh Graph View"):
        nodes, edges = get_graph_data()
        
        if not nodes:
            st.warning("No data found in Neo4j. Please upload code and build graph first.")
        else:
            # 配置图表样式
            config = Config(
                width="100%",
                height=600,
                directed=True, 
                physics=True, 
                hierarchical=False,
                nodeHighlightBehavior=True, 
                highlightColor="#F7A7A6",
                collapsible=False
            )
            
            # 渲染图谱
            agraph(nodes=nodes, edges=edges, config=config)