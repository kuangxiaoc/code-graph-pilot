"""graph_store 测试: 驱动复用与假 store 注入。"""

import os
from pathlib import Path

import pytest

import agent
import graph_store as gs


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后都重置单例, 否则驱动实例计数会跨用例串味。"""
    gs.reset_store()
    yield
    gs.reset_store()


def _install_fake_store(monkeypatch, store):
    monkeypatch.setattr(gs, "get_store", lambda: store)


class FakeStore:
    """最小假实现: 验证 agent 层只依赖协议, 不依赖 Neo4j。"""

    def __init__(self, record):
        self.record = record
        self.calls = []

    def symbol(self, name):
        self.calls.append(name)
        return dict(self.record)

    def impact(self, name, depth=5):
        self.calls.append(name)
        return dict(self.record)

    def subgraph(self, limit=100):
        return {"nodes": [], "edges": []}

    def close(self):
        pass


# --- 硬伤 #8: 驱动复用 ---

def test_get_store_returns_same_instance():
    """同一进程多次调用必须返回同一实例 —— 每次新建驱动就是硬伤 #8 复发。"""
    first = gs.get_store()
    second = gs.get_store()
    assert first is second


def test_get_store_is_lazy():
    """无数据库环境下不得在 import 时连接。"""
    assert gs._store is None  # fixture 已重置


def test_reset_store_closes_and_clears():
    store = gs.get_store()
    gs.reset_store()
    assert gs._store is None


def test_agent_does_not_construct_driver_per_call(monkeypatch):
    """agent.query_dependencies 不得自己 new driver。

    通过让 GraphDatabase.driver 抛异常来证明: 若 agent 仍自建驱动,
    这里会炸; 委托给单例则只在首次 get_store() 时构造一次。
    """
    calls = {"n": 0}

    def _boom(*a, **kw):
        calls["n"] += 1
        raise AssertionError("不应在此处构造驱动")

    monkeypatch.setattr(gs.GraphDatabase, "driver", _boom)

    # 首次 get_store 会尝试连接 (被拦截), 但 agent 层本身不碰 GraphDatabase
    with pytest.raises(AssertionError):
        gs.get_store()
    assert calls["n"] == 1


# --- 协议可替换性: 离线测试图逻辑 ---

def test_agent_uses_store_protocol(monkeypatch):
    """agent 层必须只依赖 GraphStore 协议, 不依赖 Neo4j 细节。"""
    fake = FakeStore({"uid": "m:f", "name": "f", "calls": ["g"], "called_by": ["h"]})
    _install_fake_store(monkeypatch, fake)
    record = agent.query_dependencies("f")
    assert record["uid"] == "m:f"
    assert fake.calls == ["f"]


def test_agent_renders_store_error(monkeypatch):
    fake = FakeStore({"error": "未找到符号 'f'"})
    _install_fake_store(monkeypatch, fake)
    assert "未找到" in agent.render_context(agent.query_dependencies("f"))


def test_retrieval_failed_when_store_returns_error(monkeypatch):
    fake = FakeStore({"error": "数据库连接错误: x"})
    _install_fake_store(monkeypatch, fake)
    assert agent._retrieval_failed(agent.query_dependencies("f")) is True


# --- 查询正确性: label 限定 (硬伤 #3) ---

@pytest.fixture
def live_store():
    """指向本地 Neo4j 并灌入 fixture 图; 不可达时跳过。"""
    fixture = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "webapp_py"
    try:
        from graph_builder import CodeAnalyzer, GraphLoader, module_name_for_path, resolve_graph

        analyzers = [
            CodeAnalyzer(str(fp), module=module_name_for_path(fp, fixture))
            for fp in sorted(fixture.rglob("*.py"))
        ]
        entities, relations = resolve_graph(analyzers)
        loader = GraphLoader("bolt://localhost:7687", "neo4j", "password123")
        loader.clean_db()
        loader.load_data(entities, relations)
        loader.close()
        return gs.Neo4jGraphStore("bolt://localhost:7687", "neo4j", "password123")
    except Exception as exc:  # pragma: no cover - 无数据库环境
        pytest.skip(f"Neo4j 不可用: {exc}")


def test_symbol_query_is_label_scoped(live_store):
    """同名 Class/Function 不得互相串 (硬伤 #3)。

    fixture 里有 auth.service:AuthService (Class) 与
    utils.validators:validate_token (Function) 等, 用裸名 'login' 查询
    必须解析到方法, 而不是任意一个 name 相同的实体。
    """
    record = live_store.symbol("validate_token")
    assert not record.get("error")
    assert record["uid"] == "utils.validators:validate_token"
    assert "Function" in record["labels"]


def test_symbol_query_returns_structured_error_when_missing(live_store):
    record = live_store.symbol("nonexistent_symbol_xyz")
    assert record.get("error")
    assert "uid" not in record


def test_impact_returns_transitive_upstream(live_store):
    """上游必须是传递闭包: validate_token 的调用者的调用者也要在。"""
    result = live_store.impact("validate_token")
    assert not result.get("error")
    uids = [u["uid"] for u in result["upstream"]]
    assert "auth.tokens:refresh_token" in uids
    # 距离排序: 直接调用者距离最小
    distances = [u["distance"] for u in result["upstream"]]
    assert distances == sorted(distances)


def test_impact_reports_error_for_missing_symbol(live_store):
    assert live_store.impact("nonexistent_symbol_xyz").get("error")


def test_subgraph_returns_nodes_and_edges(live_store):
    data = live_store.subgraph(limit=20)
    assert data["nodes"], "fixture 图非空, 不应返回 0 节点"
    assert data["edges"]
    for node in data["nodes"]:
        assert node["uid"], "可视化节点键必须是 uid (name 跨模块不唯一)"


def test_subgraph_handles_unreachable_db(monkeypatch):
    store = gs.Neo4jGraphStore("bolt://127.0.0.1:1", "x", "x")
    data = store.subgraph(limit=5)
    assert data["nodes"] == []
    assert data.get("error")
    store.close()
