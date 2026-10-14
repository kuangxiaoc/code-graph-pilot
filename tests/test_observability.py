"""轨迹与指标测试: 全部离线, 用假回答文本, 不调 LLM。"""

import json

from observability.trace import (
    compute_metrics,
    extract_symbols,
    record_trace,
)


def test_extracts_backticked_symbols_first():
    text = "修改 `validate_token` 会影响 `refresh_token` 和调用方。"
    assert "validate_token" in extract_symbols(text)
    assert "refresh_token" in extract_symbols(text)


def test_extracts_snake_case_identifiers():
    text = "save_to_db 会被调用"
    assert "save_to_db" in extract_symbols(text)


def test_extracts_camel_case_identifiers():
    """裸 CamelCase 词不再被抽取 (见 test_metric_normalization.py 的理由)。

    模型要指代码需打反引号; 点分限定名 (AuthService.login) 仍然有效。
    """
    text = "AuthService 的 login 方法会变"
    assert extract_symbols(text) == set()
    assert "AuthService.login" in extract_symbols("AuthService.login 方法会变")


def test_extracts_qualified_names():
    text = "auth.service:AuthService.login 是关键路径"
    symbols = extract_symbols(text)
    assert "AuthService.login" in symbols


def test_ignores_chinese_and_common_words():
    text = "这个函数的上下游依赖都需要检查一遍"
    assert extract_symbols(text) == set()


def test_metrics_zero_hallucination_when_all_grounded():
    metrics = compute_metrics(retrieved={"a", "b"}, claimed={"a", "b"})
    assert metrics["hallucination_rate"] == 0.0
    assert metrics["grounding_recall"] == 1.0
    assert metrics["hallucinated"] == []


def test_metrics_detects_hallucinated_symbol():
    metrics = compute_metrics(retrieved={"a"}, claimed={"a", "ghost"})
    assert metrics["hallucination_rate"] == 0.5
    assert metrics["hallucinated"] == ["ghost"]
    assert metrics["grounding_recall"] == 1.0


def test_metrics_detects_missed_facts():
    metrics = compute_metrics(retrieved={"a", "b"}, claimed={"a"})
    assert metrics["grounding_recall"] == 0.5
    assert metrics["missed"] == ["b"]
    assert metrics["hallucination_rate"] == 0.0


def test_metrics_empty_claimed_is_not_division_by_zero():
    metrics = compute_metrics(retrieved={"a"}, claimed=set())
    assert metrics["hallucination_rate"] == 0.0
    assert metrics["grounding_recall"] == 0.0


def test_retrieval_failure_is_not_counted_as_hallucination():
    """连不上库时不得把幻觉率算成 1.0 —— 那是把"没数据"误报成"模型编造"。"""
    metrics = compute_metrics(retrieved=set(), claimed={"a", "b"}, retrieval_failed=True)
    assert metrics["hallucination_rate"] is None
    assert metrics["grounding_recall"] is None
    assert metrics["retrieval_failed"] is True


def test_retrieval_failure_flag_defaults_false():
    metrics = compute_metrics(retrieved={"a"}, claimed={"a"})
    assert metrics["retrieval_failed"] is False
    assert metrics["hallucination_rate"] == 0.0


def test_record_trace_writes_jsonl(tmp_path):
    path = tmp_path / "traces.jsonl"
    record_trace(
        path,
        query="修改 process_data 会影响谁",
        target_func="process_data",
        nodes_visited=["parse", "retrieve", "generate", "review"],
        revision_count=0,
        review_verdicts=["PASS"],
        retrieved_symbols={"save_to_db", "validate_input"},
        response="`save_to_db` 会被影响",
        terminated_by="pass",
        duration_ms=120,
    )
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["run_id"]
    assert rec["terminated_by"] == "pass"
    assert rec["revision_count"] == 0
    assert set(rec["retrieved_symbols"]) == {"save_to_db", "validate_input"}
    assert "save_to_db" in rec["claimed_symbols"]
    assert rec["metrics"]["hallucination_rate"] == 0.0


def test_record_trace_appends(tmp_path):
    path = tmp_path / "traces.jsonl"
    for _ in range(3):
        record_trace(path, query="q", target_func="f", nodes_visited=[],
                     revision_count=0, review_verdicts=[], retrieved_symbols=set(),
                     response="", terminated_by="pass", duration_ms=1)
    assert len(path.read_text(encoding="utf-8").strip().split("\n")) == 3


def test_revision_count_and_verdicts_are_recorded(tmp_path):
    """terminated_by 必须能区分 pass 与 max_revision。"""
    path = tmp_path / "traces.jsonl"
    record_trace(path, query="q", target_func="f", nodes_visited=[],
                 revision_count=1, review_verdicts=["FAIL: 漏上游", "FAIL"],
                 retrieved_symbols={"a"}, response="`a`", terminated_by="max_revision",
                 duration_ms=5)
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["terminated_by"] == "max_revision"
    assert rec["revision_count"] == 1
    assert len(rec["review_verdicts"]) == 2


def test_trace_can_be_disabled(monkeypatch, tmp_path):
    """CODEGRAPH_TRACE=0 时必须静默跳过, 不写文件也不抛错。"""
    monkeypatch.setenv("CODEGRAPH_TRACE", "0")
    path = tmp_path / "traces.jsonl"
    record_trace(path, query="q", target_func="f", nodes_visited=[],
                 revision_count=0, review_verdicts=[], retrieved_symbols=set(),
                 response="", terminated_by="pass", duration_ms=1)
    assert not path.exists()


def test_no_api_key_in_trace(tmp_path):
    """轨迹不得泄露密钥。"""
    path = tmp_path / "traces.jsonl"
    record_trace(path, query="用 sk-abcdef123456 分析", target_func="f",
                 nodes_visited=[], revision_count=0, review_verdicts=[],
                 retrieved_symbols=set(), response="ok", terminated_by="pass",
                 duration_ms=1)
    content = path.read_text(encoding="utf-8")
    assert "sk-abcdef123456" not in content
