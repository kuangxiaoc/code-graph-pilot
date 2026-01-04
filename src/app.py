import streamlit as st
import tempfile
import os
from dotenv import load_dotenv
from graph_builder import build_graph_from_file
from agent import app_graph

load_dotenv()

st.set_page_config(page_title="CodeGraph Pilot", layout="wide")

st.title("CodeGraph Pilot ✈️")
st.markdown("### Powered by Qwen & Neo4j")

col1, col2 = st.columns([1, 2])

with col1:
    st.header("1. Ingest Code")
    uploaded_file = st.file_uploader("Upload Python File", type=["py"])
    
    if uploaded_file and st.button("Build Knowledge Graph"):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as tmp:
            tmp.write(uploaded_file.getvalue())
            tmp_path = tmp.name
        
        try:
            nodes, rels = build_graph_from_file(
                tmp_path, 
                os.getenv("NEO4J_URI"),
                os.getenv("NEO4J_USER"),
                os.getenv("NEO4J_PASSWORD")
            )
            st.success(f"✅ Graph Built! Nodes: {nodes}, Relations: {rels}")
            os.remove(tmp_path)
        except Exception as e:
            st.error(f"Error: {e}")

with col2:
    st.header("2. AI Analysis")
    query = st.text_input("Example: 'Analyze the impact of changing process_data'", "Analyze process_data")
    
    if st.button("Run Agent"):
        with st.spinner("Analyzing dependency graph..."):
            result = app_graph.invoke({"query": query})
            
            st.subheader("Graph Context (RAG):")
            st.code(result["context"], language="yaml")
            
            st.subheader("Qwen Insights:")
            st.markdown(result["response"])