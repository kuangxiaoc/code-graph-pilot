"""CLI 测试: 不调 LLM。断言输出契约与退出码。

契约来源 docs/specs/SPEC-delivery-surface.md:
- 结构化数据走 stdout, 人类可读文本不进 stdout
- 永远提供 --json
- 失败退出码非零, stderr 给可读原因
"""

import json

import pytest

import codegraph as cli


class _Store:
    """假 store: 不连数据库, 只验证 CLI 的编排与输出契约。"""

    def __init__(self, symbol_record=None, impact_record=None):
        self.symbol_record = symbol_record or {"error": "未找到符号 'x'。"}
        self.impact_record = impact_record or {"error": "未找到符号 'x'。"}
        self.calls = []

    def symbol(self, name):
        self.calls.append(("symbol", name))
        return dict(self.symbol_record)

    def impact(self, name, depth=5):
        self.calls.append(("impact", name, depth))
        return dict(self.impact_record)

    def subgraph(self, limit=100):
        return {"nodes": [], "edges": []}

    def close(self):
        pass


@pytest.fixture
def store(monkeypatch):
    fake = _Store()
    import graph_store

    monkeypatch.setattr(graph_store, "get_store", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """默认无 Key: analyze 的 happy path 用例会自行覆盖。"""
    monkeypatch.delenv("QWEN_API_KEY", raising=False)


# --- symbol ---

def test_symbol_outputs_json_to_stdout(store, capsys):
    store.symbol_record = {
        "uid": "m:f", "name": "f", "labels": ["Function"], "module": "m",
        "filepath": "/x/f.py", "lineno": 3, "calls": ["g"], "called_by": ["h"],
    }
    code = cli.main(["symbol", "f", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    # stdout 必须是纯净 JSON, 不掺人类可读文本 —— 否则 jq 直接挂
    payload = json.loads(out)
    assert payload["uid"] == "m:f"
    assert payload["calls"] == ["g"]


def test_symbol_human_output_goes_to_stdout(store, capsys):
    store.symbol_record = {
        "uid": "m:f", "name": "f", "labels": ["Function"], "module": "m",
        "filepath": "/x/f.py", "lineno": 3, "calls": [], "called_by": [],
    }
    code = cli.main(["symbol", "f"])
    out = capsys.readouterr().out
    assert code == 0
    assert "m:f" in out
    assert "无" in out  # 空依赖必须显式说明


def test_symbol_missing_returns_nonzero_and_readable_stderr(store, capsys):
    code = cli.main(["symbol", "x"])
    captured = capsys.readouterr()
    assert code == 1
    assert "错误" in captured.err
    assert "x" in captured.err
    assert captured.out == ""  # 失败时 stdout 不得有残留数据


# --- impact ---

def test_impact_shows_transitive_upstream_with_distance(store, capsys):
    store.impact_record = {
        "uid": "m:f", "name": "f", "labels": ["Function"],
        "upstream": [{"uid": "a", "distance": 1}, {"uid": "b", "distance": 2}],
        "downstream": [],
    }
    code = cli.main(["impact", "f"])
    out = capsys.readouterr().out
    assert code == 0
    assert "距离 1" in out and "距离 2" in out


def test_impact_passes_depth_through(store):
    store.impact_record = {"uid": "m:f", "name": "f", "labels": ["Function"],
                           "upstream": [], "downstream": []}
    cli.main(["impact", "f", "--depth", "3"])
    assert store.calls == [("impact", "f", 3)]


def test_impact_json_is_valid(store, capsys):
    store.impact_record = {"uid": "m:f", "name": "f", "labels": ["Function"],
                           "upstream": [], "downstream": []}
    cli.main(["impact", "f", "--json"])
    assert json.loads(capsys.readouterr().out)["uid"] == "m:f"


def test_impact_missing_returns_nonzero(store, capsys):
    assert cli.main(["impact", "x"]) == 1
    assert "错误" in capsys.readouterr().err


# --- analyze (LLM 门禁) ---

def test_analyze_without_key_gives_readable_error(monkeypatch, capsys):
    # 必须显式清空 Key: 开发者本地 .env 里通常有真 Key, 不清的话这条
    # 用例会真的去调 LLM (既慢又花 token, 且断言必然失败)。
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.setattr("agent.has_llm_credentials", lambda: False)

    code = cli.main(["analyze", "修改 f 会影响谁"])
    captured = capsys.readouterr()
    assert code == 1
    assert "QWEN_API_KEY" in captured.err
    # 必须指路: 告诉用户没有 Key 时该用什么
    assert "impact" in captured.err


def test_analyze_never_calls_llm_without_key(monkeypatch):
    """无 Key 时不得触达 LLM —— 否则用户看到的是 401 而不是提示。"""
    import agent

    def _boom(*a, **kw):
        raise AssertionError("无 Key 不应构造 LLM")

    monkeypatch.setattr(agent, "get_llm", _boom)
    assert cli.main(["analyze", "q"]) == 1


# --- report ---

def test_report_empty_traces_is_not_an_error(tmp_path, capsys):
    code = cli.main(["report", "--path", str(tmp_path / "missing.jsonl")])
    out = capsys.readouterr().out
    assert code == 0
    assert "没有轨迹" in out


def test_report_json_output(tmp_path, capsys):
    import observability.trace as trace

    path = tmp_path / "t.jsonl"
    trace.record_trace(path, query="q", target_func="f", nodes_visited=[],
                       revision_count=0, review_verdicts=[], retrieved_symbols={"a"},
                       response="`a`", terminated_by="pass", duration_ms=5)
    cli.main(["report", "--path", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_runs"] == 1
    assert payload["terminated_by"] == {"pass": 1}


def test_report_last_limits_window(tmp_path, capsys):
    import observability.trace as trace

    path = tmp_path / "t.jsonl"
    for _ in range(4):
        trace.record_trace(path, query="q", target_func="f", nodes_visited=[],
                           revision_count=0, review_verdicts=[],
                           retrieved_symbols=set(), response="",
                           terminated_by="pass", duration_ms=1)
    cli.main(["report", "--path", str(path), "--last", "2", "--json"])
    assert json.loads(capsys.readouterr().out)["total_runs"] == 2


# --- validate (CI 门禁) ---

def test_validate_fixture_passes():
    """fixture 图必须 0 违规 —— 这是 CI 门禁, 失败即回归。"""
    assert cli.main(["validate"]) == 0


def test_validate_reports_violation_count_in_json(capsys):
    cli.main(["validate", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 0
    assert payload["triples"] > 0


def test_validate_missing_fixture_returns_nonzero(capsys):
    assert cli.main(["validate", "--fixture", "nope"]) == 1
    assert "错误" in capsys.readouterr().err


# --- build ---

def test_build_missing_path_returns_nonzero(capsys):
    code = cli.main(["build", "/definitely/not/here"])
    assert code == 1
    assert "错误" in capsys.readouterr().err


def test_build_reports_skipped_files(tmp_path, monkeypatch, capsys):
    """语法错误的文件必须报出来, 而不是静默丢掉。"""
    (tmp_path / "good.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")

    import graph_builder

    class _Loader:
        def __init__(self, *a, **kw):
            pass

        def clean_db(self):
            pass

        def load_data(self, e, r):
            self.counts = (len(e), len(r))

        def close(self):
            pass

    monkeypatch.setattr(graph_builder, "GraphLoader", _Loader)
    code = cli.main(["build", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["files"] == 1
    assert len(payload["skipped"]) == 1
    assert "bad.py" in payload["skipped"][0]["file"]


def test_build_db_failure_is_readable(monkeypatch, tmp_path, capsys):
    """连不上库时必须说"容器起了吗", 而不是甩 traceback。"""
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")

    import graph_builder

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(graph_builder, "GraphLoader", _Boom)
    code = cli.main(["build", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "docker-compose" in captured.err
    assert "Traceback" not in captured.err


def test_package_parent_for_single_file_in_package(tmp_path):
    """单文件增量必须算出完整模块名, 否则会造出重复符号。

    实测 bug: 传 webapp_py/utils/validators.py 时模块名变成
    "validators" 而不是 "utils.validators", 增量重建后图里出现两个
    validate_token, symbol 查询报"匹配到多个实体"。
    """
    from codegraph import _package_parent
    from graph_builder import module_name_for_path
    from pathlib import Path

    pkg = tmp_path / "mypkg"
    (pkg / "utils").mkdir(parents=True)
    (pkg / "auth").mkdir()
    (pkg / "utils" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    f = pkg / "utils" / "validators.py"
    f.write_text("def v():\n    pass\n", encoding="utf-8")

    base = _package_parent(f)
    assert module_name_for_path(f, base) == "mypkg.utils.validators"


def test_package_parent_without_init_files(tmp_path):
    """无 __init__.py 的源码树 (fixture 形态) 也要定位到包根。"""
    from codegraph import _package_parent
    from graph_builder import module_name_for_path
    from pathlib import Path

    pkg = tmp_path / "webapp_py"
    (pkg / "utils").mkdir(parents=True)
    (pkg / "auth").mkdir()
    (pkg / "utils" / "validators.py").write_text("def v():\n    pass\n", encoding="utf-8")

    f = pkg / "utils" / "validators.py"
    base = _package_parent(f)
    assert module_name_for_path(f, base) == "utils.validators"


def test_module_name_for_single_file_does_not_crash(tmp_path):
    """root 与 filepath 相同时不得 IndexError (单文件增量的旧崩溃点)。"""
    from graph_builder import module_name_for_path
    from pathlib import Path

    f = tmp_path / "solo.py"
    f.write_text("def f():\n    pass\n", encoding="utf-8")
    # 不得抛 IndexError
    name = module_name_for_path(f, f)
    assert name == "solo"
