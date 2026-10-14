"""JSONL 轨迹 -> 汇总报告。

三条口径纪律 (与 docs/specs/SPEC-observability.md 对齐, 改了就是改结论):

1. **检索失败的 run 单独计数, 不进任何均值。** 库不可达时 retrieved 为空,
   幻觉率分子分母同时退化; 混进均值会把"连不上库"报成"模型在编造"。
2. **比率按 pooled 汇总, 不取 run 内比率的平均。** 取平均会让
   claimed_count=0 的 run (指标 0.0) 稀释整体, 而 pooled 是按符号数加权,
   与指标定义 |hallucinated|/|claimed| 同构。
3. **样本量 < MIN_SAMPLE 时明确标注不可对外宣称。** 符号抽取是下界
   (spec Open Questions), 小样本上的小数点后两位没有意义。

修正有效率是**代理指标**: 轨迹只记录最终回答, 没有记录每次 revision 前后
的 claim 集合, 因此无法直接证明"幻觉率下降了"。这里只回答一个更弱的
问题 —— 需要修正的 run 最终是否收敛到 PASS。
"""

import json
import sys
from collections import Counter
from pathlib import Path

from .trace import DEFAULT_TRACE_PATH

# 低于此样本量, 报告中的比率不得对外宣称 (见 SPEC-observability.md)
MIN_SAMPLE = 30


def load_traces(path=None):
    """读取 JSONL 轨迹, 返回 record 列表。

    坏行 (半条写入 / 手工编辑) 跳过并告警到 stderr —— 静默丢数据比报错更糟,
    因为它会让样本量看起来正常。
    """
    path = Path(path) if path else DEFAULT_TRACE_PATH
    if not path.exists():
        return []

    records, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                skipped += 1
                print(
                    f"warning: {path}:{lineno} 无法解析, 已跳过 ({exc.msg})",
                    file=sys.stderr,
                )

    if skipped:
        print(f"warning: 共跳过 {skipped} 条坏记录, 样本量偏小", file=sys.stderr)
    return records


def _percentile(values, pct):
    """线性插值分位数; values 需已排序。空列表返回 None。"""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    pos = (len(values) - 1) * pct
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    frac = pos - low
    return float(values[low] * (1 - frac) + values[high] * frac)


def summarize(records):
    """把轨迹列表汇成指标 dict。records 为空时全指标为 None, 不伪造 0。"""
    scored = [r for r in records if not r.get("retrieval_failed")]
    failed = [r for r in records if r.get("retrieval_failed")]

    metrics = [r.get("metrics") or {} for r in scored]
    claimed_total = sum(m.get("claimed_count") or 0 for m in metrics)
    retrieved_total = sum(m.get("retrieved_count") or 0 for m in metrics)
    hallucinated_total = sum(len(m.get("hallucinated") or []) for m in metrics)
    grounded_total = sum(len(m.get("grounded") or []) for m in metrics)

    # 修正有效率: 只统计真正走过 revision 的 run, 否则"从不需要修正"的
    # run 会把有效率推到 1.0, 那是在测检索质量不是在测自修正。
    revision_runs = [r for r in scored if (r.get("revision_count") or 0) >= 1]
    converged = [r for r in revision_runs if r.get("terminated_by") == "pass"]

    durations = sorted(r["duration_ms"] for r in records if r.get("duration_ms") is not None)
    offender_counts = Counter(
        symbol for m in metrics for symbol in (m.get("hallucinated") or [])
    )

    return {
        "total_runs": len(records),
        "scored_runs": len(scored),
        "retrieval_failed_runs": len(failed),
        "claimed_total": claimed_total,
        "hallucinated_total": hallucinated_total,
        "retrieved_total": retrieved_total,
        "hallucination_rate": (hallucinated_total / claimed_total) if claimed_total else None,
        "grounding_recall": (grounded_total / retrieved_total) if retrieved_total else None,
        "grounded_total": grounded_total,
        "revision_runs": len(revision_runs),
        "revision_effectiveness": (len(converged) / len(revision_runs)) if revision_runs else None,
        "terminated_by": dict(Counter(r.get("terminated_by", "unknown") for r in records)),
        "duration_ms": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "max": float(durations[-1]) if durations else None,
        },
        "top_hallucinated": offender_counts.most_common(5),
        "below_min_sample": len(scored) < MIN_SAMPLE,
        "min_sample": MIN_SAMPLE,
    }


def format_report(summary):
    """渲染成纯文本, 供 CLI 与人工阅读。"""
    lines = []
    lines.append("=== Agent 轨迹报告 ===")
    lines.append("")

    total = summary["total_runs"]
    if not total:
        lines.append("没有轨迹记录。先跑一次分析: uv run python -m agent_flow")
        return "\n".join(lines)

    lines.append(f"样本: {total} runs ({summary['scored_runs']} 计分, "
                 f"{summary['retrieval_failed_runs']} 检索失败不计分)")
    lines.append("")

    lines.append("--- 幻觉与覆盖 (pooled, 按符号数加权) ---")
    rate = summary["hallucination_rate"]
    recall = summary["grounding_recall"]
    lines.append(f"幻觉率    : {_pct(rate)}  "
                 f"({summary['hallucinated_total']} / {summary['claimed_total']} claimed)")
    lines.append(f"覆盖率    : {_pct(recall)}  "
                 f"({summary['grounded_total']} / {summary['retrieved_total']} retrieved)")

    lines.append("")
    lines.append("--- 自修正 ---")
    eff = summary["revision_effectiveness"]
    if summary["revision_runs"] == 0:
        lines.append("修正有效率: n/a  (没有任何 run 触发 revision)")
    else:
        lines.append(f"修正有效率: {_pct(eff)}  "
                     f"({summary['revision_runs']} 次 revision 中收敛到 pass)")

    lines.append("")
    lines.append("--- 终止原因 ---")
    for reason, count in sorted(summary["terminated_by"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {reason:<16} {count}")

    dur = summary["duration_ms"]
    if dur["p50"] is not None:
        lines.append("")
        lines.append("--- 耗时 ---")
        lines.append(f"p50 {dur['p50']:.0f} ms   p95 {dur['p95']:.0f} ms   "
                     f"max {dur['max']:.0f} ms")

    if summary["top_hallucinated"]:
        lines.append("")
        lines.append("--- 高频幻觉符号 (优先排查) ---")
        for symbol, count in summary["top_hallucinated"]:
            lines.append(f"  {symbol:<32} {count} 次")

    lines.append("")
    if summary["below_min_sample"]:
        lines.append(f"[警告] 计分样本 {summary['scored_runs']} < {summary['min_sample']}, "
                     f"且符号抽取是下界 (中文指代漏抽)。")
        lines.append("       以上数字仅作本地趋势观察, **不得对外宣称**。")
    else:
        lines.append("[说明] 符号抽取是下界: 回答里用\"这个函数\"指代而未写出符号名的部分")
        lines.append("       两侧都抽不到, 因此幻觉率只会被低估, 不会被高估。")

    return "\n".join(lines)


def _pct(value):
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"
