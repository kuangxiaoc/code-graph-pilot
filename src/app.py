import streamlit as st
import tempfile
import os
from dotenv import load_dotenv
from graph_builder import build_graph_from_file, build_graph_for_batch
# --- 引入可视化库 ---
# uv add streamlit-agraph
from streamlit_agraph import agraph, Node, Edge, Config

from graph_builder import build_graph_from_file
from agent import app_graph, run_analysis, render_context
from graph_store import get_store

# 加载环境变量
load_dotenv()

# --- 页面基础配置 ---
st.set_page_config(
    page_title="CodeGraph Pilot",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 工具函数：获取 Neo4j 数据用于可视化 ---
def get_graph_data(limit=100):
    """从 Neo4j 获取节点和边，转换为 Agraph 格式。

    查询收在 graph_store.subgraph(): app.py 不再直连驱动 (硬伤 #8),
    也不再各自维护一份 Cypher。
    """
    nodes = []
    edges = []
    seen = set()

    try:
        data = get_store().subgraph(limit=limit)
        if data.get("error"):
            st.error(f"⚠️ 无法连接数据库获取图谱数据: {data['error']}")
            return [], []
    except Exception as e:
        st.error(f"⚠️ 无法连接数据库获取图谱数据: {e}")
        return [], []

    for node in data["nodes"]:
        uid = node["uid"]
        if uid in seen:
            continue
        seen.add(uid)
        # 根据类型区分颜色 (Class 用紫色, Function 用红色)
        n_type = (node["labels"] or ["Node"])[0]
        color = "#9D4EDD" if "Class" in n_type else "#FF6B6B"
        nodes.append(Node(
            id=uid,
            label=node["name"],
            size=25,
            shape="dot",
            color=color,
            title=f"Module: {node.get('module') or 'N/A'}\n"
                  f"File: {node.get('filepath') or 'N/A'}\nType: {n_type}",
        ))

    for edge in data["edges"]:
        if not edge["src"] or not edge["dst"]:
            continue
        edges.append(Edge(
            source=edge["src"],
            target=edge["dst"],
            label=edge["rel"],
            color="#A0A0A0",
            font={
                "size": 10,
                "align": "middle",
                "background": "white",
                "strokeWidth": 0,
                "color": "#333333",
            },
            smooth={"type": "curvedCW", "roundness": 0.2},
        ))

    return nodes, edges


# ================= 侧边栏：控制面板 =================
with st.sidebar:
    st.title("🛠️ Project Controls")
    st.markdown("---")
    
    st.header("1. Ingestion (代码入库)")
    st.caption("Upload multiple .py files to build a project-level graph.")
    
    # ✅ 修改点 1: 允许上传多个文件
    uploaded_files = st.file_uploader(
        "Upload Python Code", 
        type=["py"], 
        accept_multiple_files=True # 开启多文件支持
    )
    
    if uploaded_files and st.button("🚀 Build Knowledge Graph", type="primary"):
        with st.status("Processing Batch...", expanded=True) as status:
            temp_paths = []
            
            # 1. 先把所有上传的文件保存到临时目录
            st.write(f"📂 Saving {len(uploaded_files)} files...")
            try:
                # 创建一个临时目录来存放这些文件
                temp_dir = tempfile.mkdtemp()
                
                for uploaded_file in uploaded_files:
                    # 获取文件名
                    file_name = uploaded_file.name
                    file_path = os.path.join(temp_dir, file_name)
                    
                    # 写入内容
                    with open(file_path, "wb") as f:
                        f.write(uploaded_file.getvalue())
                    
                    temp_paths.append(file_path)
                
                # 2. 调用批量建图函数
                st.write("🕷️ Parsing AST & Linking Dependencies...")
                nodes_count, rels_count = build_graph_for_batch(
                    temp_paths, 
                    os.getenv("NEO4J_URI"),
                    os.getenv("NEO4J_USER"),
                    os.getenv("NEO4J_PASSWORD")
                )
                
                status.update(label="✅ Batch Build Complete!", state="complete", expanded=False)
                st.success(f"Graph Built: {nodes_count} Nodes, {rels_count} Relations from {len(uploaded_files)} files.")
                
            except Exception as e:
                status.update(label="❌ Build Failed", state="error")
                st.error(f"Error: {e}")
            finally:
                # 清理临时文件
                for p in temp_paths:
                    if os.path.exists(p):
                        os.remove(p)
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
    
    st.markdown("---")
    st.markdown("### 🧠 Backend Info")
    st.info(f"LLM: **Qwen3**\nDB: **Neo4j**\nAgent: **LangGraph**")

# ================= 主界面 =================
st.title("CodeGraph Pilot ✈️")
st.markdown("#### The Structure-Aware AI Coding Assistant")

# 使用 Tabs 分离 "分析" 和 "可视化"
tab1, tab2 = st.tabs(["💬 Chat & Impact Analysis", "🕸️ Interactive Graph"])

# === Tab 1: 对话界面 (RAG + Agent) ===
with tab1:
    st.markdown("""
    <style>
    .big-font { font-size:18px !important; }
    </style>
    """, unsafe_allow_html=True)
    
    st.markdown('<p class="big-font">Ask complex questions like: <i>"Analyze the impact of modifying process_data"</i></p>', unsafe_allow_html=True)
    
    # 输入框
    col_in, col_btn = st.columns([5, 1])
    with col_in:
        query = st.text_input("Your Question:", value="Analyze the impact of modifying process_data", label_visibility="collapsed")
    with col_btn:
        run_btn = st.button("RUN ➤", type="primary", use_container_width=True)
    
    if run_btn and query:
        with st.spinner("🤖 Agent is thinking... (Retrieving Graph -> Reasoning -> Reviewing)"):
            try:
                # 调用 LangGraph Agent
                # 走 run_analysis 而不是直接 invoke: 否则 UI 是一次性调用,
                # 不产生轨迹, 成为可观测性的盲区 (报告里永远看不到 UI 流量)。
                result, trace = run_analysis(query)
                
                # 1. 展示 Agent 思考过程中的证据 (Context)
                with st.expander("🔍 Evidence from Knowledge Graph (RAG Context)", expanded=False):
                    if "context" in result:
                        record = result.get("graph_record") or {}
                        if record.get("error"):
                            st.error(record["error"])
                        else:
                            st.code(render_context(record), language="yaml")
                    else:
                        st.warning("No context retrieved.")

                # 2. 轨迹摘要: 让 UI 流量也能在 report 里被看到
                if trace:
                    metrics = trace["metrics"]
                    rate = metrics["hallucination_rate"]
                    st.caption(
                        f"终止: {trace['terminated_by']} · "
                        f"幻觉率: {'n/a (检索失败)' if rate is None else f'{rate * 100:.0f}%'} · "
                        f"耗时: {trace['duration_ms']} ms"
                    )

                # 2. 展示最终回答
                st.markdown("### 💡 Analysis Result")
                st.markdown(result["response"])
                
                # 3. 如果有 Review 反馈，可以展示（可选）
                if result.get("feedback") and result["feedback"] != "PASS":
                    st.warning(f"Note: This answer was refined based on critic feedback: {result['feedback']}")
                    
            except Exception as e:
                st.error(f"Agent Execution Error: {e}")

# === Tab 2: 图谱可视化 ===
with tab2:
    col_tools, col_graph = st.columns([1, 4])
    
    with col_tools:
        st.markdown("### Settings")
        limit = st.slider("Max Nodes", 10, 200, 50)
        physics = st.checkbox("Physics (Bounce)", value=True)
        refresh = st.button("🔄 Refresh Graph")
        
        st.markdown("---")
        st.caption("🔴 Red: Function")
        st.caption("🟣 Purple: Class")
        st.caption("🔵 Blue: Dependency")

    with col_graph:
        # 当点击刷新或初次加载时获取数据
        nodes, edges = get_graph_data(limit=limit)
        
        if not nodes:
            st.info("No graph data found. Please upload a Python file in the Sidebar first.")
        else:
            # 配置图表
            config = Config(
            width="100%",
            height=600,
            directed=True, 
            physics=physics, 
            hierarchical=False,
            nodeHighlightBehavior=True, 
            highlightColor="#F7A7A6",
            # 核心修改：全局物理引擎优化 (让节点更分散)
            node={'labelProperty': 'label'},
            link={'labelProperty': 'label', 'renderLabel': True},
            # 增加弹簧长度，防止挤在一起
            physics_settings={
                "barnesHut": {
                    "gravitationalConstant": -2000, # 斥力，越大越分散
                    "centralGravity": 0.3,
                    "springLength": 200,            # 连线长度，越长越不容易重叠
                    "springConstant": 0.04,
                    "damping": 0.09,
                    "avoidOverlap": 0.5             # 避免重叠系数
                },
                "minVelocity": 0.75
            }
        )
            
            # 渲染
            agraph(nodes=nodes, edges=edges, config=config)
