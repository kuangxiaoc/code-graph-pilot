"""MCP 入口测试: 不连 Neo4j, 用假 store 注入。

覆盖的是**分派与错误契约**, 不是 MCP 协议本身 (那是 SDK 的责任)。
agent 拿到错误时要能据此决定下一步, 因此错误必须是结构化 JSON
而不是抛异常 —— 抛异常会让 agent 直接放弃整个工具。
"""

import json

import pytest

import graph_store as gs
import mcp_server
from mcp_server import codegraph_query


class _StubStore:
    """记录调用参数, 返回可预测结果。"""

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {"uid": "m:f"}
        self.error = error
        self.calls = []

    def symbol(self, name):
        self.calls.append(("symbol", name))
        return dict(self.result)

    def impact(self, name, depth=5):
        self.calls.append(("impact", name, depth))
        return dict(self.result)

    def hierarchy(self, name):
        self.calls.append(("hierarchy", name))
        return dict(self.result)

    def subgraph(self, limit=100):
        self.calls.append(("overview", limit))
        return {"nodes": [], "edges": []}

    def close(self):
        pass


@pytest.fixture
def store(monkeypatch):
    """注入假 store, 并隔离单例状态 (否则会跨用例串味)。"""
    gs.reset_store()
    stub = _StubStore()
    monkeypatch.setattr(gs, "get_store", lambda: stub)
    yield stub
    gs.reset_store()


def _parse(raw):
    return json.loads(raw)


def test_symbol_action_dispatches_to_store(store):
    out = _parse(codegraph_query(action="symbol", name="f"))
    assert out["uid"] == "m:f"
    assert ("symbol", "f") in store.calls


def test_impact_action_clamps_depth(store):
    """depth 必须夹在 1..10 —— 传 999 会让 Cypher 遍历失控。"""
    codegraph_query(action="impact", name="f", depth=999)
    assert ("impact", "f", 10) in store.calls
    codegraph_query(action="impact", name="f", depth=0)
    assert ("impact", "f", 1) in store.calls


def test_hierarchy_and_overview_dispatch(store):
    codegraph_query(action="hierarchy", name="C")
    assert ("hierarchy", "C") in store.calls
    codegraph_query(action="overview")
    assert ("overview", 100) in store.calls


def test_unknown_action_returns_structured_error(store):
    out = _parse(codegraph_query(action="bogus"))
    assert "error" in out
    assert "symbol" in out["error"]      # 必须告诉 agent 有哪些可选


def test_missing_name_returns_structured_error(store):
    out = _parse(codegraph_query(action="symbol", name=""))
    assert "error" in out
    assert "name" in out["error"]


def test_store_error_is_passed_through_not_raised(monkeypatch):
    """store 返回 {"error": ...} 时必须原样透传。

    "符号不存在"是**预期内的查询结果**, agent 需要据此换个名字重试;
    在这里转成异常会让 agent 放弃整个工具。
    """
    gs.reset_store()
    monkeypatch.setattr(
        gs, "get_store",
        lambda: _StubStore(error="x", result={"error": "⚠️ 未找到符号 'x'。"}),
    )
    out = _parse(codegraph_query(action="symbol", name="x"))
    assert out["error"].startswith("⚠️")
    gs.reset_store()


def test_db_unreachable_returns_error_not_exception(monkeypatch):
    """连不上库时返回可读错误, 不得把 traceback 抛给 agent。"""
    gs.reset_store()

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(gs, "get_store", _boom)
    out = _parse(codegraph_query(action="symbol", name="f"))
    assert "error" in out
    assert "connection refused" in out["error"]
    gs.reset_store()


def test_tool_is_registered_with_schema():
    """工具必须注册成功且带输入 schema —— 否则 agent 看不到它。"""
    import asyncio

    async def go():
        return await mcp_server.server.list_tools()

    tools = asyncio.run(go())
    names = [t.name for t in tools]
    assert "codegraph_query" in names
    tool = next(t for t in tools if t.name == "codegraph_query")
    assert "action" in tool.input_schema["properties"]
    # description 必须写清 action 选项, 否则 agent 不知道能传什么
    assert "impact" in tool.description


def test_only_one_tool_is_exposed():
    """刻意只暴露一个工具 —— 一个强工具比一堆窄工具更能引导 agent。"""
    import asyncio

    async def go():
        return await mcp_server.server.list_tools()

    assert len(asyncio.run(go())) == 1
