"""Benchmark runner: 在 fixture 上构建图, 按 ground truth 逐场景计算 recall。

设计要点 (借鉴 cartog benchmarks):
- ground truth 由人工从源码得出, 不来自本工具输出
- 打分为 recall, 不是 accuracy: 检索系统漏召回比多召回危害大
- 索引隔离: 每次运行使用全新的内存图, 不与其他 fixture 共享状态
- known_gap 场景单独统计, 不计入总分 (诚实披露已知缺陷)

用法:
    uv run python benchmarks/runner.py                 # 打印表格
    uv run python benchmarks/runner.py --json          # 输出 JSON
    uv run python benchmarks/runner.py --fixture webapp_py
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from graph_builder import CodeAnalyzer, module_name_for_path, resolve_graph  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
GROUND_TRUTH_DIR = Path(__file__).resolve().parent / "ground_truth"


def build_graph(fixture_dir):
    """解析 fixture 目录, 返回供打分使用的邻接结构。"""
    files = sorted(str(p) for p in Path(fixture_dir).rglob("*.py"))
    if not files:
        raise FileNotFoundError(f"fixture 中未找到 .py 文件: {fixture_dir}")

    analyzers = [
        CodeAnalyzer(fp, module=module_name_for_path(fp, fixture_dir)) for fp in files
    ]
    entities, relations = resolve_graph(analyzers)

    graph = {
        "out": defaultdict(set),
        "in": defaultdict(set),
        "imports": defaultdict(set),
        "imported_by": defaultdict(set),
        "defined_in": defaultdict(set),
        "children": defaultdict(set),
        "parents": defaultdict(set),
        "symbols": set(),
        "entities": entities,
        "relations": relations,
    }

    for entity in entities:
        if entity["type"] in {"Function", "Method", "Class"}:
            graph["symbols"].add(entity["uid"])

    for rel in relations:
        src, dst, kind = rel["src"], rel["dst"], rel["rel"]
        if kind == "CALLS":
            graph["out"][src].add(dst)
            graph["in"][dst].add(src)
        elif kind == "IMPORTS":
            graph["imports"][src].add(dst)
            graph["imported_by"][dst].add(src)
        elif kind == "DEFINED_IN":
            graph["defined_in"][dst].add(src)
        elif kind == "INHERITS":
            graph["children"][dst].add(src)
            graph["parents"][src].add(dst)

    return graph


def _reachable(adjacency, start, max_depth=None):
    """从 start 出发做 BFS; 结果不含 start 自身。"""
    seen = set()
    frontier = list(adjacency.get(start, ()))
    depth = 0
    while frontier:
        if max_depth is not None and depth >= max_depth:
            break
        nxt = []
        for node in frontier:
            if node in seen or node == start:
                continue
            seen.add(node)
            nxt.extend(adjacency.get(node, ()))
        frontier = nxt
        depth += 1
    return seen


def predict(scenario, graph):
    """按场景类型返回预测到的 uid 集合。"""
    kind = scenario["kind"]
    target = scenario["target"]

    if kind == "callers":
        return set(graph["in"].get(target, ()))
    if kind == "callees":
        return set(graph["out"].get(target, ()))
    if kind == "callers_transitive":
        return _reachable(graph["in"], target)
    if kind == "callees_transitive":
        return _reachable(graph["out"], target)
    if kind == "importers":
        return set(graph["imported_by"].get(target, ()))
    if kind == "deps":
        return set(graph["imports"].get(target, ()))
    if kind == "outline":
        return set(graph["defined_in"].get(target, ()))
    if kind == "children":
        return set(graph["children"].get(target, ()))
    if kind == "hierarchy":
        return set(graph["children"].get(target, ()))
    if kind == "impact":
        # 重命名影响 = 直接调用者 + 子类 (继承该名字的地方)
        return set(graph["in"].get(target, ())) | set(graph["children"].get(target, ()))
    if kind == "search":
        needle = target.lower()
        return {uid for uid in graph["symbols"] if needle in uid.lower()}
    raise ValueError(f"未知场景类型: {kind}")


def score_scenario(scenario, graph):
    """对单个场景计算 recall。"""
    expected = set(scenario["expected"])
    predicted = predict(scenario, graph)
    hits = expected & predicted
    total = len(expected)
    recall = len(hits) / total if total else 1.0
    return {
        "recall": recall,
        "hits": len(hits),
        "total": total,
        "missed": sorted(expected - predicted),
        "extra": sorted(predicted - expected),
    }


def run_scenarios(scenarios, graph):
    """跑全部场景, 汇总宏观平均 recall, 单独统计 known_gap。"""
    results = {}
    known_gaps = 0
    for name, scenario in scenarios.items():
        if name.startswith("_"):
            continue
        if scenario.get("status") == "known_gap":
            known_gaps += 1
            continue
        results[name] = score_scenario(scenario, graph)

    scored = len(results)
    macro = sum(r["recall"] for r in results.values()) / scored if scored else 0.0
    return {
        "results": results,
        "scored": scored,
        "known_gaps": known_gaps,
        "macro_recall": macro,
        "perfect": sum(1 for r in results.values() if r["recall"] == 1.0),
    }


def evaluate_fixture(fixture="webapp_py"):
    fixture_dir = FIXTURES_DIR / fixture
    gt_path = GROUND_TRUTH_DIR / f"{fixture}.json"
    graph = build_graph(fixture_dir)
    with open(gt_path, "r", encoding="utf-8") as f:
        scenarios = json.load(f)
    summary = run_scenarios(scenarios, graph)
    summary["fixture"] = fixture
    summary["graph"] = {
        "symbols": len(graph["symbols"]),
        "entities": len(graph["entities"]),
        "relations": len(graph["relations"]),
        "dangling_calls": _count_dangling(graph),
    }
    return summary


def _count_dangling(graph):
    uids = {e["uid"] for e in graph["entities"]}
    return sum(1 for r in graph["relations"] if r["src"] not in uids or r["dst"] not in uids)


def print_report(summary):
    g = summary["graph"]
    print(f"fixture: {summary['fixture']}")
    print(f"graph: {g['symbols']} symbols, {g['relations']} relations, "
          f"{g['dangling_calls']} dangling edges")
    print(f"scored: {summary['scored']}  known_gaps(不计分): {summary['known_gaps']}")
    print()
    print(f"{'#':<4}{'场景':<26}{'recall':>8}{'hits':>7}{'total':>7}")
    print("-" * 54)
    for name in sorted(summary["results"]):
        r = summary["results"][name]
        print(f"{name[:3]:<4}{name:<26}{r['recall']:>8.3f}{r['hits']:>7}{r['total']:>7}")
        if r["missed"]:
            for m in r["missed"]:
                print(f"      遗漏: {m}")
    print("-" * 54)
    print(f"{'macro recall':<30}{summary['macro_recall']:>8.3f}")
    print(f"{'满分场景':<30}{summary['perfect']:>8}/{summary['scored']}")


def main():
    parser = argparse.ArgumentParser(description="运行代码图 benchmark")
    parser.add_argument("--fixture", default="webapp_py")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    summary = evaluate_fixture(args.fixture)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=list))
    else:
        print_report(summary)
    return 0 if summary["macro_recall"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
