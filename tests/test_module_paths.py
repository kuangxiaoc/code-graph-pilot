"""包内模块命名测试: 目录结构必须映射为点分模块名, 否则跨文件导入全部失配。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graph_builder import CodeAnalyzer, module_name_for_path, resolve_graph  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "webapp_py"


def test_nested_path_maps_to_dotted_module():
    root = "/proj"
    assert module_name_for_path("/proj/utils/validators.py", root) == "utils.validators"
    assert module_name_for_path("/proj/auth/tokens.py", root) == "auth.tokens"
    assert module_name_for_path("/proj/main.py", root) == "main"


def test_package_init_maps_to_package_name():
    root = "/proj"
    assert module_name_for_path("/proj/auth/__init__.py", root) == "auth"


def test_fixture_imports_resolve_instead_of_becoming_external():
    """fixture 内的跨文件导入必须解析成真实节点, 不得退化成 External 桩。"""
    files = sorted(str(p) for p in FIXTURE.rglob("*.py"))
    analyzers = [
        CodeAnalyzer(fp, module=module_name_for_path(fp, FIXTURE)) for fp in files
    ]
    entities, _ = resolve_graph(analyzers)

    stubs = [e for e in entities if e["type"] == "External"]
    # 只允许标准库/第三方 (无 "auth|services|utils" 前缀)
    internal_stubs = [
        e for e in stubs
        if e["uid"].split(":")[0].split(".")[0] in {"auth", "services", "utils"}
    ]
    assert internal_stubs == [], f"fixture 内部模块被误判为外部: {internal_stubs}"


def test_fixture_cross_module_calls_are_real_edges():
    files = sorted(str(p) for p in FIXTURE.rglob("*.py"))
    analyzers = [
        CodeAnalyzer(fp, module=module_name_for_path(fp, FIXTURE)) for fp in files
    ]
    entities, relations = resolve_graph(analyzers)
    uids = {e["uid"] for e in entities}

    calls = [r for r in relations if r["rel"] == "CALLS"]
    dangling = [r for r in calls if r["dst"] not in uids]
    assert dangling == [], f"悬空调用边: {dangling}"

    # 具体验证 ground truth 里的关键边
    pairs = {(r["src"], r["dst"]) for r in calls}
    assert ("auth.tokens:refresh_token", "utils.validators:validate_token") in pairs
    assert ("auth.service:AuthService.login", "services.db:persist") in pairs
    assert ("routes.api:login_route", "auth.middleware:authenticate") in pairs


def test_fixture_has_no_uid_collisions():
    files = sorted(str(p) for p in FIXTURE.rglob("*.py"))
    analyzers = [
        CodeAnalyzer(fp, module=module_name_for_path(fp, FIXTURE)) for fp in files
    ]
    entities, _ = resolve_graph(analyzers)
    uids = [e["uid"] for e in entities]
    assert len(uids) == len(set(uids))
