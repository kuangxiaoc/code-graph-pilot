"""真实仓库评测: 在任意 Python 仓库上量化图的解析质量。

与 benchmarks/runner.py 的分工:
- runner.py 在 fixture 上打 **recall** (有人工 ground truth, 可判对错)
- 本脚本在真实仓库上打 **覆盖率与残差** (无 ground truth, 只报告事实)

为什么不能直接用 runner.py: 真实仓库没有人工标注, 无法判定"这条边该不该
有"。因此这里只报告可验证的结构性事实, 并把 unresolved 拆成
"本仓库定义过" (真漏检) 与"外部/内置" (合理), 让残差可读。

用法:
    uv run python benchmarks/repo_eval.py /path/to/repo
    uv run python benchmarks/repo_eval.py /tmp/requests/src/requests --json
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from graph_builder import CodeAnalyzer, resolve_graph  # noqa: E402
from runner import module_name_for_path  # noqa: E402


# dict/str/list/bytes/file 等内置类型的常见方法。这些名字恰好也可能在本
# 仓库被定义 (例如仓库里有个 get()), 因此必须按名字形态先摘出来,
# 否则会把 dict.get 误报成漏检。
_BUILTIN_METHODS = {
    "get", "items", "keys", "values", "update", "pop", "setdefault", "copy",
    "clear", "append", "extend", "insert", "remove", "index", "count", "sort",
    "split", "join", "strip", "lstrip", "rstrip", "lower", "upper", "replace",
    "startswith", "endswith", "find", "format", "encode", "decode", "read",
    "write", "close", "seek", "tell", "flush", "readlines", "hexdigest",
    "digest", "exists", "isdir", "isfile", "makedirs", "items",
}


def evaluate(repo_root, package_root=None):
    """在真实仓库上构建图并返回质量指标。"""
    root = Path(repo_root).resolve()
    # package_root 用于算模块名: 解析 src/requests 时模块应是 requests.xxx
    # 而不是 src.requests.xxx, 否则与 import 语句对不上。
    base = Path(package_root).resolve() if package_root else root.parent

    files = sorted(str(p) for p in root.rglob("*.py"))
    analyzers = [CodeAnalyzer(f, module=module_name_for_path(f, base)) for f in files]
    entities, relations = resolve_graph(analyzers)

    uids = {e["uid"] for e in entities}
    external = {e["uid"] for e in entities if e["type"] == "External"}
    real_defs = {e["uid"] for e in entities
                 if e["type"] in ("Function", "Method", "Class")}

    calls = [r for r in relations if r["rel"] == "CALLS"]
    resolved = [r for r in calls if r["dst"] in real_defs]
    stubbed = [r for r in calls if r["dst"] in external]
    dangling = [r for r in relations if r["dst"] not in uids]

    # unresolved 拆成三类。只按"名字是否定义过"分两类是不够的:
    # dict.get / str.split 这类内置方法名恰好也可能在本仓库出现, 会被
    # 误算成漏检 (实测 requests 里 33 个 get 全是 dict.get)。
    # 因此先看名字是否属于内置容器/类型的常见方法, 再判是否定义过。
    defined_names = {u.rsplit(":", 1)[-1].rsplit(".", 1)[-1] for u in real_defs}
    counter = Counter()
    for a in analyzers:
        for u in a.unresolved_calls:
            counter[u["name"]] += 1

    builtin_like = {n: c for n, c in counter.items() if n in _BUILTIN_METHODS}
    rest = {n: c for n, c in counter.items() if n not in _BUILTIN_METHODS}
    missed = {n: c for n, c in rest.items() if n in defined_names}
    external_names = {n: c for n, c in rest.items() if n not in defined_names}

    return {
        "repo": str(root),
        "files": len(files),
        "symbols": len(real_defs),
        "nodes": len(entities),
        "relations": len(relations),
        "calls_total": len(calls),
        "calls_resolved": len(resolved),
        "calls_to_stub": len(stubbed),
        "resolution_rate": round(len(resolved) / len(calls), 4) if calls else 0.0,
        "dangling_edges": len(dangling),
        "unresolved_total": sum(counter.values()),
        "unresolved_missed": sum(missed.values()),
        "unresolved_external": sum(external_names.values()),
        "unresolved_builtin": sum(builtin_like.values()),
        "missed_top": sorted(missed.items(), key=lambda kv: -kv[1])[:15],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="真实仓库图质量评测")
    ap.add_argument("repo", help="仓库根目录")
    ap.add_argument("--package-root", default=None,
                    help="包根目录 (用于算模块名); 省略则取 repo 的父目录")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = evaluate(args.repo, args.package_root)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"仓库: {result['repo']}")
    print(f"文件 {result['files']}  符号 {result['symbols']}  "
          f"节点 {result['nodes']}  关系 {result['relations']}")
    print()
    print(f"CALLS 边        {result['calls_total']}")
    print(f"  -> 真实定义   {result['calls_resolved']}")
    print(f"  -> External 桩 {result['calls_to_stub']}")
    print(f"解析率          {result['resolution_rate']:.1%}")
    print(f"悬空边          {result['dangling_edges']}  (必须为 0)")
    print()
    print(f"unresolved 调用 {result['unresolved_total']}")
    print(f"  内置容器方法 (合理) {result['unresolved_builtin']}")
    print(f"  第三方/外部 (合理)  {result['unresolved_external']}")
    print(f"  真漏检              {result['unresolved_missed']}")
    if result["missed_top"]:
        print()
        print("真漏检 top:")
        for name, n in result["missed_top"]:
            print(f"  {n:4d}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
