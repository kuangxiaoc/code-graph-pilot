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
# 全部用假 driver 拦截 execute_query, 不连 Neo4j —— 见 tests/conftest.py 的
# FakeDriver。理由: AGENTS.md 要求所有测试离线, 且连库测试使整套测试从
# 约 3 秒涨到 149 秒 (连接重试的指数退避是主因)。


def _store_with(monkeypatch, records):
    """构造一个 execute_query 返回给定 records 的 store。"""
    store = gs.Neo4jGraphStore.__new__(gs.Neo4jGraphStore)
    store.uri = "bolt://fake"
    store.user = store.password = "x"
    store._driver = _FakeDriver(records)
    return store


class _FakeDriver:
    """按调用次序返回 records; 传单个列表则每次都返回它。

    impact() 会发两条查询 (symbol 定位 + 变长路径), 二者期望数据不同,
    因此支持传 [[symbol 记录], [上游记录]] 形式按次序取。
    """

    def __init__(self, records):
        self.records = records
        self.queries = []

    def _records_for(self, index):
        if self.records and isinstance(self.records[0], list):
            seq = self.records[min(index, len(self.records) - 1)]
            return seq
        return self.records

    def execute_query(self, query, **params):
        self.queries.append((query, params))
        return _FakeResult(self._records_for(len(self.queries) - 1))

    def close(self):
        pass


class _FakeResult:
    def __init__(self, records):
        self.records = records


class _FakeRecord(dict):
    """模拟 neo4j Record: 支持 r["uid"] 与 r.uid 两种访问。"""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


def _rec(**kw):
    return _FakeRecord(kw)


def test_symbol_query_is_label_scoped(monkeypatch):
    """同名 Class/Function 不得互相串 (硬伤 #3)。

    查询必须带 label 白名单 (Function|Method|Class), 否则
    MATCH (n {name: $name}) 会让 :Class 与 :Function 同名实体互相串。
    """
    store = _store_with(monkeypatch, [
        _rec(uid="utils.validators:validate_token", name="validate_token",
             module="utils.validators", filepath="/x/v.py", lineno=8,
             labels=["Function"], calls=[], called_by=["auth.tokens:refresh_token"]),
    ])
    record = store.symbol("validate_token")
    assert not record.get("error")
    assert record["uid"] == "utils.validators:validate_token"
    assert record["labels"] == ["Function"]

    # 关键断言: 查询里必须限定 label
    query = store._driver.queries[0][0]
    assert "Function|Method|Class" in query
    # 且不得出现跨 label 的宽松匹配
    assert "MATCH (target)" in query and "target:" in query


def test_symbol_query_reports_multiple_matches(monkeypatch):
    """多个匹配必须报错而不是任选一个 —— 猜一个就是假确定性。"""
    store = _store_with(monkeypatch, [
        _rec(uid="a:f", name="f", module="a", filepath=None, lineno=None,
             labels=["Function"], calls=[], called_by=[]),
        _rec(uid="b:f", name="f", module="b", filepath=None, lineno=None,
             labels=["Function"], calls=[], called_by=[]),
    ])
    record = store.symbol("f")
    assert "匹配到多个实体" in record["error"]
    assert "uid" not in record


def test_symbol_query_returns_structured_error_when_missing(monkeypatch):
    store = _store_with(monkeypatch, [])
    record = store.symbol("nonexistent_symbol_xyz")
    assert record.get("error")
    assert "uid" not in record


def test_symbol_filters_none_from_collect(monkeypatch):
    """Cypher collect() 无匹配时返回 [None], 不得进 retrieved 集合。"""
    store = _store_with(monkeypatch, [
        _rec(uid="a:f", name="f", module="a", filepath=None, lineno=None,
             labels=["Function"], calls=[None], called_by=[None]),
    ])
    record = store.symbol("f")
    assert record["calls"] == []
    assert record["called_by"] == []


def test_symbol_handles_connection_failure(monkeypatch):
    """连接失败必须转为结构化 error, 不得抛异常打断工作流。"""

    class _Boom:
        def execute_query(self, *a, **kw):
            raise RuntimeError("connection refused")

    store = gs.Neo4jGraphStore.__new__(gs.Neo4jGraphStore)
    store._driver = _Boom()
    record = store.symbol("f")
    assert "数据库连接错误" in record["error"]


def test_impact_returns_transitive_upstream_with_distance(monkeypatch):
    """上游必须是传递闭包, 且带距离 (排序后才能看出风险梯度)。"""
    store = _store_with(monkeypatch, [
        [_rec(uid="a:f", name="f", module="a", filepath=None, lineno=None,
              labels=["Function"], calls=["g"], called_by=["b"])],
        [_rec(uid="b", distance=1), _rec(uid="c", distance=2)],
    ])
    result = store.impact("f")

    assert not result.get("error")
    assert [u["uid"] for u in result["upstream"]] == ["b", "c"]
    assert [u["distance"] for u in result["upstream"]] == [1, 2]

    # 变长路径查询必须用 WHERE 定位起点 —— Neo4j 不允许参数出现在 MATCH 模式。
    # queries[0] 是 symbol() 的定位查询, 变长路径在 queries[1]。
    assert len(store._driver.queries) == 2
    path_query = store._driver.queries[1][0]
    assert "CALLS*1.." in path_query
    assert "WHERE start.uid = $uid" in path_query


def test_impact_depth_is_interpolated_not_parameterized(monkeypatch):
    """深度必须白名单化后拼接: Cypher 的变长路径上界不接受参数。"""
    store = _store_with(monkeypatch, [
        [_rec(uid="a:f", name="f", module="a", filepath=None, lineno=None,
              labels=["Function"], calls=[], called_by=[])],
        [],  # 变长路径无上游
    ])
    store.impact("f", depth=3)
    assert "CALLS*1..3" in store._driver.queries[1][0]


def test_impact_reports_error_for_missing_symbol(monkeypatch):
    store = _store_with(monkeypatch, [])
    assert store.impact("nonexistent_symbol_xyz").get("error")


def test_impact_propagates_symbol_error(monkeypatch):
    """上游查询失败时错误必须冒泡, 而不是返回半份数据。"""

    class _Boom:
        def __init__(self):
            self.calls = 0

        def execute_query(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                return _FakeResult([_rec(uid="a:f", name="f", module="a",
                                         filepath=None, lineno=None,
                                         labels=["Function"], calls=[], called_by=[])])
            raise RuntimeError("boom")

    store = gs.Neo4jGraphStore.__new__(gs.Neo4jGraphStore)
    store._driver = _Boom()
    assert "数据库连接错误" in store.impact("f")["error"]


def test_subgraph_returns_nodes_and_edges(monkeypatch):
    """可视化节点键必须是 uid: name 跨模块不唯一 (硬伤 #3 的可视化侧)。"""
    store = _store_with(monkeypatch, [])
    store._driver.records = [
        (_FakeRecord({"n": _FakeNode("a:f", "f", "a"),
                      "r": _FakeRel("CALLS"),
                      "m": _FakeNode("b:g", "g", "b")})),
    ]
    data = store.subgraph(limit=20)
    assert [n["uid"] for n in data["nodes"]] == ["a:f", "b:g"]
    assert data["edges"] == [{"src": "a:f", "rel": "CALLS", "dst": "b:g"}]


def test_subgraph_deduplicates_nodes(monkeypatch):
    """同一节点在多条边里出现时只入队一次。"""
    store = _store_with(monkeypatch, [])
    store._driver.records = [
        _FakeRecord({"n": _FakeNode("a:f", "f", "a"), "r": _FakeRel("CALLS"),
                     "m": _FakeNode("b:g", "g", "b")}),
        _FakeRecord({"n": _FakeNode("b:g", "g", "b"), "r": _FakeRel("CALLS"),
                     "m": _FakeNode("c:h", "h", "c")}),
    ]
    data = store.subgraph(limit=20)
    assert len(data["nodes"]) == 3
    assert len(data["edges"]) == 2


def test_subgraph_handles_unreachable_db():
    """连不上库时返回空图 + error, 不得让 UI 崩。"""

    class _Boom:
        def execute_query(self, *a, **kw):
            raise RuntimeError("refused")

    store = gs.Neo4jGraphStore.__new__(gs.Neo4jGraphStore)
    store._driver = _Boom()
    data = store.subgraph(limit=5)
    assert data["nodes"] == []
    assert data.get("error")


class _FakeNode(dict):
    def __init__(self, uid, name, module):
        super().__init__(uid=uid, name=name, module=module, filepath=None, lineno=None)
        self.labels = ["Function"]

    def get(self, k, default=None):
        return dict.get(self, k, default)


class _FakeRel:
    def __init__(self, type_):
        self.type = type_
