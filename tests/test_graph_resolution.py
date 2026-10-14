"""跨文件解析 (resolve_graph) 与加载校验测试：不依赖 Neo4j。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graph_builder import CodeAnalyzer, resolve_graph, GraphLoader  # noqa: E402


def uids(entities):
    return {e["uid"] for e in entities}


def calls(relations):
    return {(r["src"], r["dst"]) for r in relations if r["rel"] == "CALLS"}


def test_cross_module_call_resolves_between_real_nodes():
    """A 调用 B 里定义的函数时, 必须连成真实节点间的 CALLS 边。"""
    a = CodeAnalyzer("from b import helper\n\ndef use():\n    helper()\n", module="a")
    b = CodeAnalyzer("def helper():\n    pass\n", module="b")

    entities, relations = resolve_graph([a, b])
    assert ("a:use", "b:helper") in calls(relations)


def test_call_to_external_module_creates_stub_not_silent_drop():
    """调用未解析的外部模块 (os.getenv) 时, 不得静默丢边。"""
    a = CodeAnalyzer("import os\n\ndef f():\n    os.getenv('X')\n", module="a")

    entities, relations = resolve_graph([a])
    assert ("a:f", "os:getenv") in calls(relations)
    stubs = [e for e in entities if e["uid"] == "os:getenv"]
    assert len(stubs) == 1
    assert stubs[0]["type"] == "External"


def test_shared_external_module_deduplicates():
    """两个文件都 import os 时, :Module os 只能有一个节点。"""
    a = CodeAnalyzer("import os\n", module="a")
    b = CodeAnalyzer("import os\n", module="b")

    entities, _ = resolve_graph([a, b])
    assert [e["uid"] for e in entities].count("os") == 1


def test_no_call_edge_is_lost_in_resolution():
    """resolve_graph 后的 CALLS 边数应等于各文件本地调用 + 外部调用之和。"""
    analyzers = [
        CodeAnalyzer("def a1():\n    pass\n\ndef a2():\n    a1()\n", module="m1"),
        CodeAnalyzer("import os\n\ndef b1():\n    os.getenv('X')\n", module="m2"),
    ]
    expected = sum(
        len([r for r in a.relations if r["rel"] == "CALLS"])
        + len([r for r in a.external_relations if r["rel"] == "CALLS"])
        for a in analyzers
    )
    _, relations = resolve_graph(analyzers)
    assert len([r for r in relations if r["rel"] == "CALLS"]) == expected
    assert expected == 2


def test_inherits_from_imported_class_does_not_dangle():
    """class QueryState(TypedDict) 的基类来自 typing, 不得悬空丢边。"""
    a = CodeAnalyzer(
        "from typing import TypedDict\n\nclass S(TypedDict):\n    pass\n", module="a"
    )
    entities, relations = resolve_graph([a])
    inherits = [r for r in relations if r["rel"] == "INHERITS"]
    assert len(inherits) == 1
    assert inherits[0]["dst"] == "typing:TypedDict"
    assert any(e["uid"] == "typing:TypedDict" for e in entities)


def test_resolve_graph_deduplicates_relations():
    """同一调用重复出现时只保留一条边。"""
    a = CodeAnalyzer(
        "def t():\n    pass\n\ndef c():\n    t()\n    t()\n", module="m"
    )
    _, relations = resolve_graph([a])
    assert len([r for r in relations if r["rel"] == "CALLS"]) == 1


def test_loader_rejects_missing_endpoint(monkeypatch):
    """加载器必须对找不到端点的边报错, 而不是静默跳过 (旧的静默丢边教训)。"""
    loader = GraphLoader.__new__(GraphLoader)   # 不触发真实连接
    ran = []

    class FakeSession:
        def run(self, query, **kwargs):
            ran.append(query)
            return self

        def consume(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    loader.driver = type("D", (), {"session": lambda self: FakeSession()})()

    entities = [{"type": "Module", "uid": "m", "name": "m",
                 "module": "m", "filepath": None, "lineno": 1, "external": False}]
    relations = [{"src": "m", "rel": "CALLS", "dst": "does_not_exist"}]

    with pytest.raises(ValueError, match="does_not_exist"):
        loader.load_data(entities, relations)


def test_loader_rejects_illegal_label():
    loader = GraphLoader.__new__(GraphLoader)
    entities = [{"type": "NotALabel", "uid": "x", "name": "x"}]
    with pytest.raises(ValueError, match="NotALabel"):
        loader.load_data(entities, [])
