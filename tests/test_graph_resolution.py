"""跨文件解析 (resolve_graph) 与加载校验测试：不依赖 Neo4j。"""

from pathlib import Path

import pytest

from graph_builder import CodeAnalyzer, resolve_graph, GraphLoader

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


# --- 增量更新 (硬伤 #5) ---
# 全部离线: 用假 driver 拦截 Cypher, 不连 Neo4j。

def _fake_loader(monkeypatch, run_log):
    """构造不连库的 GraphLoader, 记录每条执行的 Cypher。"""
    from graph_builder import GraphLoader

    loader = GraphLoader.__new__(GraphLoader)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, query, **params):
            run_log.append((query, params))
            return _Consumed()

    class _Driver:
        def session(self):
            return _Session()

        def close(self):
            pass

    loader.driver = _Driver()
    return loader


class _Consumed:
    def consume(self):
        return self

    def single(self):
        return {"deleted": 1}


def test_drop_modules_deletes_only_that_module_subgraph(monkeypatch):
    """按 uid 前缀删除, 不得波及其他模块的同名符号 (硬伤 #3 的删除侧)。"""
    from graph_builder import GraphLoader

    log = []
    loader = _fake_loader(monkeypatch, log)

    loader.drop_modules(["pkg.caller"])

    assert len(log) == 1
    query, params = log[0]
    # 删除条件必须同时覆盖 Module 节点自身与以 "module:" 开头的符号
    assert params["uid"] == "pkg.caller"
    assert params["prefix"] == "pkg.caller:"
    assert "DETACH DELETE" in query
    # 按 uid 前缀而不是按 name: 同名符号跨模块大量存在
    assert "name" not in query


def test_drop_modules_is_a_noop_for_empty_input(monkeypatch):
    """空输入不得发查询 —— 无变更文件时不该打库。"""
    from graph_builder import GraphLoader

    log = []
    loader = _fake_loader(monkeypatch, log)
    assert loader.drop_modules([]) == 0
    assert log == []


def test_incremental_build_drops_stale_subgraph(monkeypatch, tmp_path):
    """端到端复现硬伤 #5: 源码里改名后, --keep 不得留下幽灵节点与幽灵边。

    这是实测过的那一幕: build -> 改名 -> build --keep, 旧符号及其 CALLS 边
    曾残留在图里, 影响分析会报告一个已不存在的调用者。
    """
    import codegraph as cli

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "validators.py").write_text(
        "def validate_token(t):\n    return bool(t)\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "caller.py").write_text(
        "from pkg.validators import validate_token\n\n\ndef entry():\n"
        "    return validate_token('x')\n",
        encoding="utf-8",
    )

    operations = []

    class _Loader:
        def __init__(self, *a, **kw):
            pass

        def clean_db(self):
            operations.append("clean_db")

        def drop_modules(self, modules):
            operations.append(("drop_modules", tuple(sorted(modules))))

        def load_data(self, entities, relations):
            operations.append(("load_data", len(entities), len(relations)))

        def close(self):
            pass

    monkeypatch.setattr("graph_builder.GraphLoader", _Loader)

    # 首次 build: 全库重建
    assert cli.main(["build", str(tmp_path), "--json"]) == 0
    assert operations[0] == "clean_db"

    # 改名后增量 build: 必须先删旧子图再写新数据
    (tmp_path / "pkg" / "caller.py").write_text(
        "from pkg.validators import validate_token\n\n\ndef renamed_entry():\n"
        "    return validate_token('x')\n",
        encoding="utf-8",
    )
    operations.clear()
    assert cli.main(["build", str(tmp_path), "--keep", "--json"]) == 0

    assert operations[0] == ("drop_modules", ("pkg.caller", "pkg.validators"))
    # 顺序很重要: 先删后写, 否则 MERGE 会把幽灵节点留下来
    assert operations[1][0] == "load_data"
