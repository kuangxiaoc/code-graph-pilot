"""报告层测试: 口径纪律必须被测试守护, 否则下次"优化"就会悄悄改掉。

重点覆盖三条纪律:
1. 检索失败的 run 不计入任何比率均值
2. 比率是 pooled (按符号数加权), 不是 run 内比率的平均
3. 样本量 < MIN_SAMPLE 时明确标注不可宣称
"""

import json

from observability.report import MIN_SAMPLE, format_report, load_traces, summarize


def _write_traces(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _record(**over):
    rec = {
        "run_id": "r",
        "query": "q",
        "target_func": "f",
        "nodes_visited": ["parse", "retrieve", "generate", "review"],
        "revision_count": 0,
        "review_verdicts": ["PASS"],
        "terminated_by": "pass",
        "duration_ms": 100,
        "retrieved_symbols": [],
        "claimed_symbols": [],
        "retrieval_failed": False,
        "metrics": {
            "hallucination_rate": 0.0,
            "grounding_recall": 1.0,
            "hallucinated": [],
            "grounded": [],
            "missed": [],
            "claimed_count": 0,
            "retrieved_count": 0,
            "retrieval_failed": False,
        },
    }
    rec.update(over)
    return rec


def _with_metrics(hallucinated=(), grounded=(), missed=(), retrieval_failed=False):
    return {
        "hallucination_rate": (len(hallucinated) / (len(hallucinated) + len(grounded)))
        if (hallucinated or grounded) else 0.0,
        "grounding_recall": (len(grounded) / (len(grounded) + len(missed)))
        if (grounded or missed) else 0.0,
        "hallucinated": list(hallucinated),
        "grounded": list(grounded),
        "missed": list(missed),
        "claimed_count": len(hallucinated) + len(grounded),
        "retrieved_count": len(grounded) + len(missed),
        "retrieval_failed": retrieval_failed,
    }


# --- 纪律 1: 检索失败单独计数 ---

def test_retrieval_failed_runs_are_excluded_from_rates(tmp_path):
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(metrics=_with_metrics(hallucinated=["ghost"], grounded=["a"])),
        _record(retrieval_failed=True,
                metrics=_with_metrics(hallucinated=["x", "y"], retrieval_failed=True)),
    ])
    summary = summarize(load_traces(path))
    assert summary["total_runs"] == 2
    assert summary["scored_runs"] == 1
    assert summary["retrieval_failed_runs"] == 1
    # 失败的 run 有 2 个幻觉符号, 若混入均值会把幻觉率从 0.5 推到 0.75
    assert summary["hallucination_rate"] == 0.5


def test_all_runs_failed_reports_none_not_zero(tmp_path):
    """全部检索失败时指标是 None, 不是 0.0 —— 0.0 会被读成"没有幻觉"。"""
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(retrieval_failed=True,
                metrics=_with_metrics(hallucinated=["x"], retrieval_failed=True)),
    ])
    summary = summarize(load_traces(path))
    assert summary["hallucination_rate"] is None
    assert summary["grounding_recall"] is None


def test_empty_traces_reports_none_not_zero(tmp_path):
    summary = summarize(load_traces(tmp_path / "missing.jsonl"))
    assert summary["total_runs"] == 0
    assert summary["hallucination_rate"] is None
    assert "没有轨迹" in format_report(summary)


# --- 纪律 2: pooled 而非平均 ---

def test_rates_are_pooled_not_mean_of_rates(tmp_path):
    """两个 run: 一个 0/1 幻觉, 一个 4/4 幻觉。

    pooled = 4/5 = 0.8; run 内平均 = 0.5。取平均会让大样本 run 的严重
    幻觉被稀释, 这在"检测模型退化"的方向上是错的。
    """
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(metrics=_with_metrics(hallucinated=[], grounded=["a"])),
        _record(metrics=_with_metrics(hallucinated=["p", "q", "r", "s"], grounded=[])),
    ])
    summary = summarize(load_traces(path))
    assert summary["hallucination_rate"] == 0.8


def test_pooled_recall_uses_symbol_counts(tmp_path):
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(metrics=_with_metrics(grounded=["a"], missed=["b", "c", "d"])),
    ])
    summary = summarize(load_traces(path))
    assert summary["grounding_recall"] == 0.25


# --- 纪律 3: 样本量门槛 ---

def test_below_min_sample_flags_unreportable(tmp_path):
    records = [_record(metrics=_with_metrics(grounded=["a"])) for _ in range(3)]
    path = _write_traces(tmp_path / "t.jsonl", records)
    summary = summarize(load_traces(path))
    assert summary["below_min_sample"] is True
    assert "不得对外宣称" in format_report(summary)


def test_at_min_sample_no_warning(tmp_path):
    records = [_record(metrics=_with_metrics(grounded=["a"])) for _ in range(MIN_SAMPLE)]
    path = _write_traces(tmp_path / "t.jsonl", records)
    summary = summarize(load_traces(path))
    assert summary["below_min_sample"] is False
    text = format_report(summary)
    assert "不得对外宣称" not in text
    assert "下界" in text


# --- 自修正代理指标 ---

def test_revision_effectiveness_only_counts_revision_runs(tmp_path):
    """没触发 revision 的 run 不得计入有效率, 否则会把有效率推到 1.0。"""
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(terminated_by="pass", revision_count=0),
        _record(terminated_by="pass", revision_count=1),
        _record(terminated_by="max_revision", revision_count=1),
    ])
    summary = summarize(load_traces(path))
    assert summary["revision_runs"] == 2
    assert summary["revision_effectiveness"] == 0.5


def test_revision_effectiveness_none_when_no_revisions(tmp_path):
    path = _write_traces(tmp_path / "t.jsonl", [_record(revision_count=0)])
    summary = summarize(load_traces(path))
    assert summary["revision_effectiveness"] is None
    assert "n/a" in format_report(summary)


# --- 终止原因与耗时 ---

def test_terminated_by_distribution(tmp_path):
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(terminated_by="pass"),
        _record(terminated_by="pass"),
        _record(terminated_by="max_revision"),
    ])
    summary = summarize(load_traces(path))
    assert summary["terminated_by"] == {"pass": 2, "max_revision": 1}


def test_duration_percentiles(tmp_path):
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(duration_ms=10), _record(duration_ms=20),
        _record(duration_ms=30), _record(duration_ms=40),
    ])
    dur = summarize(load_traces(path))["duration_ms"]
    assert dur["p50"] == 25.0
    assert dur["max"] == 40.0
    assert dur["p95"] is not None


def test_top_hallucinated_ranks_by_frequency(tmp_path):
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(metrics=_with_metrics(hallucinated=["a", "b"])),
        _record(metrics=_with_metrics(hallucinated=["a"])),
    ])
    top = summarize(load_traces(path))["top_hallucinated"]
    assert top[0] == ("a", 2)
    assert dict(top)["b"] == 1


# --- 健壮性 ---

def test_malformed_lines_are_skipped_with_warning(tmp_path, capsys):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"run_id": "ok"}\n'
        '{"run_id": "truncated"\n'
        '\n',
        encoding="utf-8",
    )
    records = load_traces(path)
    assert len(records) == 1
    assert "无法解析" in capsys.readouterr().err


def test_format_report_includes_all_three_metrics(tmp_path):
    path = _write_traces(tmp_path / "t.jsonl", [
        _record(metrics=_with_metrics(hallucinated=["g"], grounded=["a"], missed=["b"])),
    ])
    text = format_report(summarize(load_traces(path)))
    assert "幻觉率" in text
    assert "覆盖率" in text
    assert "修正" in text
    assert "g" in text  # 高频幻觉符号要能点名
