"""benchmark runner 的离线单元测试: 用内存图断言打分逻辑, 不连 Neo4j。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

from runner import (  # noqa: E402
    _count_dangling,
    build_graph,
    evaluate_fixture,
    run_scenarios,
    score_scenario,
)


def make_graph():
    """手工构造一个可核对的小图, 用作打分基准。"""
    return {
        "out": {
            "a:x": {"b:y"},
            "b:y": {"c:z"},
        },
        "in": {
            "b:y": {"a:x"},
            "c:z": {"b:y"},
        },
        "imports": {"a": {"b"}},
        "imported_by": {"b": {"a"}},
        "defined_in": {
            "a": {"a:x"},
            "b": {"b:y"},
            "c": {"c:z"},
        },
        "children": {},
        "parents": {},
        "symbols": {"a:x", "b:y", "c:z"},
    }


def test_callers_scores_hits_and_misses():
    graph = make_graph()
    scenario = {
        "kind": "callers",
        "target": "b:y",
        "expected": ["a:x", "missing:sym"],
    }
    result = score_scenario(scenario, graph)
    assert result["recall"] == 0.5
    assert result["hits"] == 1
    assert result["total"] == 2


def test_perfect_recall_when_all_expected_present():
    graph = make_graph()
    scenario = {"kind": "callers", "target": "b:y", "expected": ["a:x"]}
    result = score_scenario(scenario, graph)
    assert result["recall"] == 1.0


def test_callees_transitive_walks_multiple_hops():
    graph = make_graph()
    scenario = {
        "kind": "callees_transitive",
        "target": "a:x",
        "expected": ["b:y", "c:z"],
    }
    assert score_scenario(scenario, graph)["recall"] == 1.0


def test_callers_transitive_walks_upwards():
    graph = make_graph()
    scenario = {
        "kind": "callers_transitive",
        "target": "c:z",
        "expected": ["b:y", "a:x"],
    }
    assert score_scenario(scenario, graph)["recall"] == 1.0


def test_search_is_case_insensitive_substring_over_uid():
    graph = make_graph()
    scenario = {"kind": "search", "target": "B:Y", "expected": ["b:y"]}
    assert score_scenario(scenario, graph)["recall"] == 1.0


def test_outline_lists_symbols_of_module():
    graph = make_graph()
    scenario = {"kind": "outline", "target": "a", "expected": ["a:x"]}
    assert score_scenario(scenario, graph)["recall"] == 1.0


def test_known_gap_scenarios_are_excluded_from_aggregate():
    graph = make_graph()
    scenarios = {
        "s1": {"kind": "callers", "target": "b:y", "expected": ["a:x"]},
        "s2": {"kind": "callers", "target": "b:y", "expected": ["nope"],
               "status": "known_gap"},
    }
    summary = run_scenarios(scenarios, graph)
    assert summary["scored"] == 1
    assert summary["known_gaps"] == 1
    assert summary["macro_recall"] == 1.0


def test_meta_key_is_ignored():
    graph = make_graph()
    scenarios = {
        "_meta": {"fixture": "x"},
        "s1": {"kind": "callers", "target": "b:y", "expected": ["a:x"]},
    }
    assert run_scenarios(scenarios, graph)["scored"] == 1


def test_fixture_graph_has_no_dangling_edges():
    """回归保护: fixture 图上不允许出现悬空边。"""
    graph = build_graph(
        Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "webapp_py"
    )
    assert _count_dangling(graph) == 0


def test_fixture_recall_does_not_regress():
    """回归保护: 宏观 recall 不得低于当前基线。"""
    summary = evaluate_fixture("webapp_py")
    assert summary["scored"] == 12
    assert summary["macro_recall"] >= 0.94, summary["macro_recall"]
