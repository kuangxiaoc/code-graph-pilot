"""
CodeGraph Pilot v2.0 - 增强版

新增功能：
- 目录路径输入
- 语义搜索
- 层级布局可视化
"""

import streamlit as st
import tempfile
import os
from dotenv import load_dotenv
from neo4j import GraphDatabase
from typing import List, Dict, Any

# --- 引入可视化库 ---
from streamlit_agraph import agraph, Node, Edge, Config

# --- 引入自定义模块 ---
from graph_builder import build_graph_for_batch, build_graph_from_directory, scan_project_directory
from agent import app_graph, get_call_depths
from embedding import build_embedding_index, semantic_search, get_embedding_store

# 加载环境变量
load_dotenv()

# --- 页面基础配置 ---
st.set_page_config(
    page_title="CodeGraph Pilot v2.0",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Session State 初始化 ---
if "last_entities" not in st.session_state:
    st.session_state.last_entities = []


# --- 工具函数：获取 Neo4j 数据用于可视化 ---
def get_graph_data(limit=100, use_hierarchy=False):
    """从 Neo4j 获取节点和边，转换为 Agraph 格式"""
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    pwd = os.getenv("NEO4J_PASSWORD")
    
    nodes = []
    edges = []
    node_ids = set()
    
    # 查询：获取前 N 个关系
    query = f"""
    MATCH (n)-[r]->(m)
    RETURN n, r, m
    LIMIT {limit}
    """
    
    # 获取调用深度（用于层级布局）
    call_depths = get_call_depths() if use_hierarchy else {}
    
    try:
        with GraphDatabase.driver(uri, auth=(user, pwd)) as driver:
            results = driver.execute_query(query).records
            
            for record in results:
                src = record['n']
                dst = record['m']
                rel = record['r']
                
                # --- 处理源节点 ---
                if src['name'] not in node_ids:
                    n_type = list(src.labels)[0] if src.labels else "Node"
                    color = "#9D4EDD" if "Class" in n_type else "#FF6B6B"
                    
                    # 层级布局：使用深度作为 y 坐标
                    level = call_depths.get(src['name'], 0) if use_hierarchy else None
                    
                    nodes.append(Node(
                        id=src['name'], 
                        label=src['name'], 
                        size=25, 
                        shape="dot",
                        color=color,
                        title=f"File: {src.get('filepath', 'N/A')}\nType: {n_type}",
                        level=level
                    ))
                    node_ids.add(src['name'])
                
                # --- 处理目标节点 ---
                if dst['name'] not in node_ids:
                    n_type = list(dst.labels)[0] if dst.labels else "Node"
                    color = "#9D4EDD" if "Class" in n_type else "#4D96FF"
                    
                    level = call_depths.get(dst['name'], 0) if use_hierarchy else None
                    
                    nodes.append(Node(
                        id=dst['name'], 
                        label=dst['name'], 
                        size=20, 
                        shape="dot",
                        color=color,
                        level=level
                    ))
                    node_ids.add(dst['name'])
                
                # --- 处理边 ---
                edges.append(Edge(
                    source=src['name'], 
                    target=dst['name'], 
                    label=rel.type, 
                    color="#A0A0A0",
                    font={
                        "size": 10,
                        "align": "middle",
                        "background": "white",
                        "strokeWidth": 0,
                        "color": "#333333"
                    },
                    smooth={"type": "curvedCW", "roundness": 0.2} 
                ))
                
    except Exception as e:
        st.error(f"⚠️ 无法连接数据库获取图谱数据: {e}")
        return [], []
            
    return nodes, edges


# ================= 侧边栏：控制面板 =================
with st.sidebar:
    st.title("🛠️ Project Controls")
    st.markdown("---")
    
    # ========== 方式 1: 目录路径输入 ==========
    st.header("1. 目录扫描")
    st.caption("输入项目目录路径，自动扫描所有 .py 文件")
    
    dir_path = st.text_input("项目目录路径", placeholder="/path/to/your/project")
    
    if dir_path and st.button("📂 扫描目录", type="primary"):
        if os.path.isdir(dir_path):
            py_files = scan_project_directory(dir_path)
            st.session_state.scanned_files = py_files
            st.success(f"找到 {len(py_files)} 个 Python 文件")
            with st.expander(f"查看文件列表 ({len(py_files)} 个)"):
                for f in py_files[:20]:
                    st.text(f)
                if len(py_files) > 20:
                    st.text(f"... 还有 {len(py_files) - 20} 个文件")
        else:
            st.error("目录不存在")
    
    # 如果有扫描结果，显示构建按钮
    if "scanned_files" in st.session_state and st.session_state.scanned_files:
        if st.button("🚀 构建知识图谱", type="primary"):
            with st.status("正在构建知识图谱...", expanded=True) as status:
                try:
                    st.write(f"📁 处理 {len(st.session_state.scanned_files)} 个文件...")
                    
                    nodes_count, rels_count, entities = build_graph_for_batch(
                        st.session_state.scanned_files,
                        os.getenv("NEO4J_URI"),
                        os.getenv("NEO4J_USER"),
                        os.getenv("NEO4J_PASSWORD")
                    )
                    
                    st.write("🔢 构建向量索引...")
                    indexed_count = build_embedding_index(entities)
                    
                    st.session_state.last_entities = entities
                    
                    status.update(label="✅ 构建完成!", state="complete", expanded=False)
                    st.success(f"图谱: {nodes_count} 节点, {rels_count} 关系 | 向量索引: {indexed_count} 函数")
                    
                except Exception as e:
                    status.update(label="❌ 构建失败", state="error")
                    st.error(f"错误: {e}")
    
    st.markdown("---")
    
    # ========== 方式 2: 文件上传 ==========
    st.header("2. 文件上传")
    st.caption("手动上传 .py 文件")
    
    uploaded_files = st.file_uploader(
        "上传 Python 文件", 
        type=["py"], 
        accept_multiple_files=True
    )
    
    if uploaded_files and st.button("🚀 上传并构建", type="secondary"):
        with st.status("处理上传文件...", expanded=True) as status:
            temp_paths = []
            temp_dir = tempfile.mkdtemp()
            
            try:
                st.write(f"📂 保存 {len(uploaded_files)} 个文件...")
                
                for uploaded_file in uploaded_files:
                    file_path = os.path.join(temp_dir, uploaded_file.name)
                    with open(file_path, "wb") as f:
                        f.write(uploaded_file.getvalue())
                    temp_paths.append(file_path)
                
                st.write("🕷️ 解析 AST & 构建依赖关系...")
                nodes_count, rels_count, entities = build_graph_for_batch(
                    temp_paths, 
                    os.getenv("NEO4J_URI"),
                    os.getenv("NEO4J_USER"),
                    os.getenv("NEO4J_PASSWORD")
                )
                
                st.write("🔢 构建向量索引...")
                indexed_count = build_embedding_index(entities)
                
                st.session_state.last_entities = entities
                
                status.update(label="✅ 构建完成!", state="complete", expanded=False)
                st.success(f"图谱: {nodes_count} 节点, {rels_count} 关系 | 向量索引: {indexed_count} 函数")
                
            except Exception as e:
                status.update(label="❌ 构建失败", state="error")
                st.error(f"错误: {e}")
            finally:
                for p in temp_paths:
                    if os.path.exists(p):
                        os.remove(p)
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
    
    st.markdown("---")
    st.markdown("### 🧠 后端信息")
    st.info(f"LLM: **Qwen3**\nDB: **Neo4j**\nAgent: **LangGraph**\n向量: **FAISS**")


# ================= 主界面 =================
st.title("CodeGraph Pilot ✈️")
st.markdown("#### 结构感知的 AI 代码助手 v2.0")

# 使用 Tabs 分离功能
tab1, tab2, tab3 = st.tabs(["💬 智能问答", "🔍 语义搜索", "🕸️ 图谱可视化"])

# === Tab 1: 智能问答 ===
with tab1:
    st.markdown("""
    支持多种查询方式：
    - **影响分析**: "修改 process_data 函数会影响哪些代码?"
    - **调用链查询**: "从 main 到 process_data 的调用路径是什么?"
    - **函数信息**: "process_data 函数是做什么的?"
    """)
    
    col_in, col_btn = st.columns([5, 1])
    with col_in:
        query = st.text_input("输入问题:", placeholder="修改 process_data 函数会影响哪些代码?", label_visibility="collapsed")
    with col_btn:
        run_btn = st.button("提交", type="primary", use_container_width=True)
    
    if run_btn and query:
        with st.spinner("🤖 Agent 思考中..."):
            try:
                result = app_graph.invoke({"query": query})
                
                # 展示检索上下文
                with st.expander("🔍 检索上下文", expanded=False):
                    st.code(result.get("context", "无"), language="yaml")
                
                # 展示回答
                st.markdown("### 💡 分析结果")
                st.markdown(result["response"])
                
            except Exception as e:
                st.error(f"执行错误: {e}")

# === Tab 2: 语义搜索 ===
with tab2:
    st.markdown("用自然语言描述你想要的功能，系统会找到最相关的函数。")
    
    search_query = st.text_input("描述你想要的功能:", placeholder="处理 JSON 数据的函数")
    top_k = st.slider("返回结果数", 1, 10, 5)
    
    if st.button("🔍 搜索", type="primary"):
        if search_query:
            with st.spinner("搜索中..."):
                results = semantic_search(search_query, top_k=top_k)
                
                if results:
                    st.markdown(f"### 找到 {len(results)} 个相关函数")
                    
                    for i, r in enumerate(results, 1):
                        with st.container():
                            col1, col2 = st.columns([3, 1])
                            with col1:
                                st.markdown(f"**{i}. `{r['name']}`**")
                                st.caption(f"📁 {r['filepath']}:{r['lineno']}")
                            with col2:
                                st.metric("相似度", f"{r['similarity']:.2f}")
                            
                            if r['docstring']:
                                st.markdown(f"📄 {r['docstring'][:200]}...")
                            
                            with st.expander("查看源码"):
                                st.code(r['source'], language="python")
                            
                            st.markdown("---")
                else:
                    st.warning("未找到相关函数。请确保已构建向量索引（需要设置 DASHSCOPE_API_KEY）。")
        else:
            st.warning("请输入搜索内容")

# === Tab 3: 图谱可视化 ===
with tab3:
    col_tools, col_graph = st.columns([1, 4])
    
    with col_tools:
        st.markdown("### 设置")
        limit = st.slider("最大节点数", 10, 200, 50)
        use_hierarchy = st.checkbox("层级布局", value=True)
        physics = st.checkbox("物理动画", value=False)
        
        if st.button("🔄 刷新图谱"):
            st.rerun()
        
        st.markdown("---")
        st.caption("🟣 紫色: Class")
        st.caption("🔴 红色: Function")
        st.caption("🔵 蓝色: Method")
        st.markdown("---")
        st.caption("层级布局按调用深度排列\n顶层: 入口函数\n底层: 底层工具函数")

    with col_graph:
        nodes, edges = get_graph_data(limit=limit, use_hierarchy=use_hierarchy)
        
        if not nodes:
            st.info("暂无图谱数据。请在侧边栏上传项目目录或文件。")
        else:
            config = Config(
                width="100%",
                height=600,
                directed=True, 
                physics=physics, 
                hierarchical=use_hierarchy,
                nodeHighlightBehavior=True, 
                highlightColor="#F7A7A6",
                node={'labelProperty': 'label'},
                link={'labelProperty': 'label', 'renderLabel': True},
            )
            
            # 层级布局配置
            if use_hierarchy:
                config.physics_settings = {
                    "hierarchicalRepulsion": {
                        "nodeDistance": 150,
                        "springLength": 100
                    }
                }
            else:
                config.physics_settings = {
                    "barnesHut": {
                        "gravitationalConstant": -2000,
                        "centralGravity": 0.3,
                        "springLength": 200,
                        "springConstant": 0.04,
                        "damping": 0.09
                    }
                }
            
            agraph(nodes=nodes, edges=edges, config=config)
