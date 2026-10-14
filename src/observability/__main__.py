"""observability 命令行入口。

用法:
    uv run python -m observability report              # 汇总全部轨迹
    uv run python -m observability report --last 20     # 只看最近 20 条
    uv run python -m observability report --json        # 机器可读输出
    uv run python -m observability paths                # 打印当前轨迹文件位置
"""

import argparse
import json
import sys
from pathlib import Path

from .report import format_report, load_traces, summarize
from .trace import DEFAULT_TRACE_PATH


def cmd_report(args):
    records = load_traces(args.path)
    if args.last:
        records = records[-args.last:]

    summary = summarize(records)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        # 样本不足时仍以非零退出吗? 不: 报告成功生成了, 只是不可宣称。
        # 把"数据不足"编码成退出码会让 CI 无法区分"没数据"和"指标变差"。
        return 0

    print(format_report(summary))
    return 0


def cmd_paths(args):
    path = Path(args.path) if args.path else DEFAULT_TRACE_PATH
    print(path.resolve())
    print(f"存在: {'是' if path.exists() else '否'}"
          f"{'' if path.exists() else '  (还没有任何轨迹, 先跑一次分析)'}")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="observability", description="Agent 轨迹分析")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_report = sub.add_parser("report", help="汇总轨迹并输出指标")
    p_report.add_argument("--path", default=None, help="轨迹 JSONL 路径")
    p_report.add_argument("--last", type=int, default=None, help="只统计最近 N 条")
    p_report.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    p_report.set_defaults(func=cmd_report)

    p_paths = sub.add_parser("paths", help="打印当前轨迹文件位置")
    p_paths.add_argument("--path", default=None)
    p_paths.set_defaults(func=cmd_paths)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
