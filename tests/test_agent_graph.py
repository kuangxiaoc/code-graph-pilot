"""Agent 模块的离线可测性: 不连 Neo4j、不调 LLM。"""

from pathlib import Path

import agent

def test_agent_module_imports_without_api_key():
    """agent.py 必须能在无 QWEN_API_KEY 时被导入, 否则图逻辑无法离线测试。"""
    assert agent.app_graph is not None
    assert callable(agent.query_dependencies)

def test_workflow_has_expected_nodes():
    nodes = set(agent.app_graph.get_graph().nodes)
    assert {"parse", "retrieve", "generate", "review"} <= nodes

def test_query_dependencies_reports_connection_error_gracefully(monkeypatch):
    """数据库不可达时应返回可读错误字符串, 而不是抛异常打断工作流。"""
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("NEO4J_USER", "x")
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    result = agent.query_dependencies("anything")
    assert isinstance(result, str)
    assert "错误" in result or "未找到" in result
