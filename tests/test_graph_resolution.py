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


# --- 局部变量类型推断 (原硬伤 #4 剩余一半) ---
#
# `obj.method()` 此前一律记 unresolved, 于是 repo.save() / svc.refresh()
# 这类最常见的调用形态全部丢失。benchmark 的 11_deep_callees 场景 recall
# 只有 0.545, 遗漏的 5 个符号全是这个原因; 修完 12/12 满分。

def test_local_var_type_resolves_obj_method():
    """`repo = Repository()` 之后 `repo.save()` 必须解析到真实方法。"""
    a = CodeAnalyzer(
        "from services.db import Repository\n\n"
        "def f():\n    repo = Repository()\n    repo.save()\n",
        module="a",
    )
    b = CodeAnalyzer(
        "class Repository:\n    def save(self):\n        pass\n",
        module="services.db",
    )
    entities, relations = resolve_graph([a, b])
    assert ("a:f", "services.db:Repository.save") in calls(relations)
    assert a.unresolved_calls == []


def test_inherited_method_resolves_via_base_class():
    """子类调用继承来的方法, 必须解析到**父类**的方法节点。"""
    a = CodeAnalyzer(
        "class Base:\n    def audit(self):\n        pass\n"
        "class Child(Base):\n    def run(self):\n        pass\n"
        "def f():\n    c = Child()\n    c.audit()\n",
        module="m",
    )
    entities, relations = resolve_graph([a])
    assert ("m:f", "m:Base.audit") in calls(relations)


def test_self_call_resolves_to_inherited_method():
    """self.audit() 中 audit 定义在父类时, 不得造出 Child.audit 桩。"""
    a = CodeAnalyzer(
        "class Base:\n    def audit(self):\n        pass\n"
        "class Child(Base):\n    def run(self):\n        self.audit()\n",
        module="m",
    )
    entities, relations = resolve_graph([a])
    assert ("m:Child.run", "m:Base.audit") in calls(relations)
    # 不得凭空造出 Child.audit
    assert not any("Child.audit" in r["dst"] for r in relations)


def test_unknown_var_type_stays_unresolved():
    """类型推断不出来的调用, 不得伪造 —— 宁可 unresolved 也不能乱连边。"""
    a = CodeAnalyzer("def g(x):\n    x.unknown_method()\n", module="m")
    entities, relations = resolve_graph([a])
    assert not any(r["rel"] == "CALLS" and "unknown_method" in r["dst"]
                   for r in relations)
    assert a.unresolved_calls


def test_cross_module_var_type_resolves_to_real_method():
    """跨模块的 svc = AuthService(); svc.audit() 必须找到真实方法节点。

    单文件解析看不见 AuthService 的方法表, 且 audit 继承自 BaseService ——
    必须等节点归并后按 BELONGS_TO/INHERITS 反查, 不能拼 uid 猜。
    """
    svc = CodeAnalyzer(
        "class BaseService:\n    def audit(self):\n        pass\n"
        "class AuthService(BaseService):\n    def login(self):\n        pass\n",
        module="auth.service",
    )
    route = CodeAnalyzer(
        "from auth.service import AuthService\n\n"
        "def admin_route():\n    s = AuthService()\n    return s.audit('x')\n",
        module="routes.admin",
    )
    entities, relations = resolve_graph([svc, route])
    assert ("routes.admin:admin_route",
            "auth.service:BaseService.audit") in calls(relations)
    # audit 继承自 BaseService, 不得造出 AuthService.audit 桩
    assert not any(e["uid"] == "auth.service:AuthService.audit"
                   for e in entities)


# --- 参数类型标注推断 (真实仓库评测驱动) ---
#
# requests 内部大量调用走参数而非局部赋值 (def f(self, r: Response)),
# 且全用相对导入 (from .models import Response)。两处任一缺失都会让
# 标注形同虚设 —— 实测只做 visit_Assign 时真实 CALLS 边只有 128 条,
# 补上标注 + 相对导入后到 239 条。

def test_relative_import_resolves_to_absolute_module():
    """`from .models import Response` 必须解析成 pkg.models, 不是 ".models"。"""
    a = CodeAnalyzer("from .models import Response\n\n"
                     "def f(r: Response):\n    return r.json()\n",
                     module="pkg.auth", filepath="pkg/auth.py")
    b = CodeAnalyzer("class Response:\n    def json(self):\n        pass\n",
                     module="pkg.models", filepath="pkg/models.py")
    entities, relations = resolve_graph([a, b])
    assert ("pkg.auth:f", "pkg.models:Response.json") in calls(relations)


def test_relative_import_in_init_file_points_to_itself():
    """__init__.py 里 level=1 指向自身包, 不是父包。"""
    a = CodeAnalyzer("from .models import Response\n",
                     module="pkg", filepath="pkg/__init__.py")
    assert "Response" in a._import_aliases
    assert a._import_aliases["Response"][0] == "pkg.models"


def test_param_annotation_resolves_call():
    """带类型标注的参数, 其方法调用必须解析到真实符号。"""
    a = CodeAnalyzer(
        "from mymod import Thing\n\n"
        "def f(t: Thing):\n    return t.run()\n",
        module="m",
    )
    b = CodeAnalyzer("class Thing:\n    def run(self):\n        pass\n",
                     module="mymod")
    entities, relations = resolve_graph([a, b])
    assert ("m:f", "mymod:Thing.run") in calls(relations)


def test_union_annotation_is_not_guessed():
    """`X | None` 复合标注不得取任一分支 —— 连错比不连更糟。"""
    a = CodeAnalyzer(
        "from mymod import A, B\n\n"
        "def f(t: A | B):\n    return t.run()\n",
        module="m",
    )
    b = CodeAnalyzer("class A:\n    def run(self):\n        pass\n"
                     "class B:\n    def run(self):\n        pass\n",
                     module="mymod")
    entities, relations = resolve_graph([a, b])
    # A.run / B.run 都不该被连上: 无法判定 t 是哪一个
    assert not any(r["rel"] == "CALLS" and r["dst"].endswith(".run")
                   for r in relations)


def test_function_result_is_not_treated_as_type():
    """`cookie_jar = cast(...)` 不得把 cast 当成类型。"""
    a = CodeAnalyzer(
        "from typing import cast\nfrom mycookies import CookieJar\n\n"
        "def f():\n    jar = cast(CookieJar, None)\n    jar.set_cookie()\n",
        module="m",
    )
    entities, relations = resolve_graph([a])
    # cast(...) 自身作为一次调用连到 typing:cast 是对的;
    # 但不得把 cast 当成 jar 的类型, 进而造出 typing:cast.set_cookie
    assert not any(r["dst"].endswith("cast.set_cookie")
                   for r in relations if r["rel"] == "CALLS")
    # jar 的类型未知, 因此 set_cookie 必须留在 unresolved 而不是乱连
    assert any(u["name"] == "set_cookie" for u in a.unresolved_calls)
