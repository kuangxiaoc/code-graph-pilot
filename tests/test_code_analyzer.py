"""CodeAnalyzer 单文件解析测试：不依赖 Neo4j。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graph_builder import CodeAnalyzer  # noqa: E402


def entities_of(analyzer, kind=None):
    items = analyzer.entities
    if kind:
        items = [e for e in items if e["type"] == kind]
    return items


def relations_of(analyzer, rel):
    return [r for r in analyzer.relations if r["rel"] == rel]


def test_module_entity_is_created():
    """每个被解析的文件都应产生一个 :Module 节点，否则无法表达文件级依赖。"""
    analyzer = CodeAnalyzer("def f():\n    pass\n", module="pkg.mod")
    modules = entities_of(analyzer, "Module")
    assert len(modules) == 1
    assert modules[0]["name"] == "pkg.mod"


def test_symbols_carry_module_and_uid():
    """符号需要 module 归属和全局唯一 uid，否则同名实体跨文件会互相覆盖。"""
    analyzer = CodeAnalyzer("def f():\n    pass\n", module="pkg.mod")
    func = entities_of(analyzer, "Function")[0]
    assert func["module"] == "pkg.mod"
    assert func["uid"] == "pkg.mod:f"


def test_symbol_is_defined_in_its_module():
    analyzer = CodeAnalyzer("def f():\n    pass\n", module="pkg.mod")
    defined_in = relations_of(analyzer, "DEFINED_IN")
    assert len(defined_in) == 1
    assert defined_in[0]["src"] == "pkg.mod:f"
    assert defined_in[0]["dst"] == "pkg.mod"


def test_self_method_call_resolves_to_qualified_name():
    """self.save() 必须解析成 Class.save，而不是裸 save。"""
    src = (
        "class Repo:\n"
        "    def save(self):\n"
        "        pass\n"
        "    def flush(self):\n"
        "        self.save()\n"
    )
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    calls = relations_of(analyzer, "CALLS")
    assert len(calls) == 1
    assert calls[0]["src"] == "pkg.mod:Repo.flush"
    assert calls[0]["dst"] == "pkg.mod:Repo.save"


def test_inherits_is_class_to_class():
    src = "class A:\n    pass\n\nclass B(A):\n    pass\n"
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    inherits = relations_of(analyzer, "INHERITS")
    assert len(inherits) == 1
    assert inherits[0]["src"] == "pkg.mod:B"
    assert inherits[0]["dst"] == "pkg.mod:A"
    # 本体内已定义 A，应能判定为 Class，而非猜测。
    assert inherits[0]["dst_kind"] == "Class"


def test_import_creates_module_edges():
    """import 必须产生 Module -> Module 的 IMPORTS 边，当前完全缺失。

    注意: 跨模块边存在 analyzer.external_relations, 由 resolve_graph 定稿。
    """
    src = "import os\nfrom auth.service import validate_token\n"
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    imports = [r for r in analyzer.external_relations if r["rel"] == "IMPORTS"]
    targets = {r["dst"] for r in imports}
    assert "os" in targets
    assert "auth.service" in targets
    assert all(r["src"] == "pkg.mod" for r in imports)


def test_attribute_call_on_unknown_object_is_not_fabricated():
    """source.save() 无法确定类型时不得伪造 CALLS 边，应记为未解析。"""
    src = "def f(source):\n    source.save()\n"
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    assert relations_of(analyzer, "CALLS") == []
    assert len(analyzer.unresolved_calls) == 1
    unresolved = analyzer.unresolved_calls[0]
    assert unresolved["name"] == "save"
    assert unresolved["caller"] == "pkg.mod:f"


def test_dynamic_getattr_call_is_not_fabricated():
    src = "def f(obj):\n    getattr(obj, 'save')()\n"
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    assert relations_of(analyzer, "CALLS") == []


def test_same_name_symbols_in_different_modules_do_not_collide():
    a = CodeAnalyzer("def save():\n    return 1\n", module="pkg.a")
    b = CodeAnalyzer("def save():\n    return 2\n", module="pkg.b")
    assert entities_of(a, "Function")[0]["uid"] != entities_of(b, "Function")[0]["uid"]


def test_repeated_calls_are_deduplicated():
    src = (
        "def target():\n"
        "    pass\n"
        "def caller():\n"
        "    target()\n"
        "    target()\n"
    )
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    calls = relations_of(analyzer, "CALLS")
    assert len(calls) == 1


def test_class_method_naming_is_preserved():
    src = "class Repo:\n    def save(self):\n        pass\n"
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    method = entities_of(analyzer, "Method")[0]
    assert method["name"] == "Repo.save"
    assert method["uid"] == "pkg.mod:Repo.save"


def test_belongs_to_links_method_to_class():
    src = "class Repo:\n    def save(self):\n        pass\n"
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    belongs = relations_of(analyzer, "BELONGS_TO")
    assert len(belongs) == 1
    assert belongs[0]["src"] == "pkg.mod:Repo.save"
    assert belongs[0]["dst"] == "pkg.mod:Repo"


def test_async_functions_are_parsed():
    src = "async def fetch():\n    pass\n"
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    funcs = entities_of(analyzer, "Function")
    assert len(funcs) == 1
    assert funcs[0]["uid"] == "pkg.mod:fetch"


def test_nested_function_does_not_steal_class_scope():
    src = (
        "class Repo:\n"
        "    def save(self):\n"
        "        def helper():\n"
        "            pass\n"
        "        helper()\n"
    )
    analyzer = CodeAnalyzer(src, module="pkg.mod")
    uid = "pkg.mod:Repo.save.helper"
    funcs = [e for e in analyzer.entities if e["uid"] == uid]
    assert len(funcs) == 1
    assert funcs[0]["type"] == "Function"


def test_syntax_error_raises_cleanly():
    with pytest.raises(SyntaxError):
        CodeAnalyzer("def f(:\n", module="pkg.mod")
