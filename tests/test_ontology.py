"""本体层测试: 全部离线, 用 rdflib 内存图, 不连 Neo4j。"""

from pathlib import Path

import pytest

from ontology.schema import (
    NS,
    CLASS_LABELS,
    REL_TYPES,
    load_schema,
)
from ontology.schema import RDFS
from ontology.export import graph_to_rdf
from ontology.validate import validate_graph

def valid_neo4j_graph():
    """一份结构正确的图 (entities + relations), 用于导出。"""
    entities = [
        {"type": "Module", "uid": "m", "name": "m", "module": "m",
         "filepath": "m.py", "lineno": 1, "external": False, "kind": "module"},
        {"type": "Class", "uid": "m:Repo", "name": "Repo", "module": "m",
         "filepath": "m.py", "lineno": 3, "external": False, "kind": "class"},
        {"type": "Method", "uid": "m:Repo.save", "name": "Repo.save", "module": "m",
         "filepath": "m.py", "lineno": 4, "external": False, "kind": "method"},
        {"type": "Function", "uid": "m:f", "name": "f", "module": "m",
         "filepath": "m.py", "lineno": 9, "external": False, "kind": "function"},
        # IMPORTS 的 target 必须是已声明的 Module 节点 (真实图中由
        # resolve_graph 创建 :External 桩, 不能漏声明)
        {"type": "External", "uid": "other", "name": "other", "module": "other",
         "filepath": None, "lineno": None, "external": True, "kind": "module"},
    ]
    relations = [
        {"src": "m:Repo", "rel": "DEFINED_IN", "dst": "m"},
        {"src": "m:Repo.save", "rel": "BELONGS_TO", "dst": "m:Repo"},
        {"src": "m:f", "rel": "DEFINED_IN", "dst": "m"},
        {"src": "m:Repo.save", "rel": "CALLS", "dst": "m:f"},
        {"src": "m", "rel": "IMPORTS", "dst": "other"},
    ]
    return {"entities": entities, "relations": relations}

def test_schema_declares_all_labels():
    schema = load_schema()
    for label in CLASS_LABELS:
        assert (NS[label], None, None) in schema, f"{label} 未在 schema 中声明"

def test_every_label_is_a_subclass_of_symbol():
    """类层级必须挂在 :Symbol 下, 否则 SPARQL 无法统一查询。"""
    schema = load_schema()
    for label in CLASS_LABELS:
        assert (NS[label], RDFS.subClassOf, NS.Symbol) in schema

def test_every_relation_has_domain_and_range():
    for rel in REL_TYPES:
        schema = load_schema()
        prop = NS[rel]
        assert (prop, RDFS.domain, None) in schema, f"{rel} 缺 domain"
        assert (prop, RDFS.range, None) in schema, f"{rel} 缺 range"

def test_exports_valid_graph_without_violations():
    g = graph_to_rdf(valid_neo4j_graph())
    report = validate_graph(g)
    assert report.violations == [], report.describe()

def test_inherits_domain_violation_is_detected():
    """INHERITS 的 domain/range 必须是 Class。Function 参与继承应被报出。"""
    g = valid_neo4j_graph()
    g["relations"].append({"src": "m:f", "rel": "INHERITS", "dst": "m:Repo"})
    report = validate_graph(graph_to_rdf(g))
    assert any("INHERITS" in v["rule"] for v in report.violations), report.describe()

def test_belongs_to_domain_violation_is_detected():
    """BELONGS_TO 的 domain 必须是 Method。"""
    g = valid_neo4j_graph()
    g["relations"].append({"src": "m:f", "rel": "BELONGS_TO", "dst": "m:Repo"})
    report = validate_graph(graph_to_rdf(g))
    assert any("BELONGS_TO" in v["rule"] for v in report.violations), report.describe()

def test_imports_range_violation_is_detected():
    """IMPORTS 的 range 必须是 Module; 指向 Function 应被报出。"""
    g = valid_neo4j_graph()
    g["relations"].append({"src": "m", "rel": "IMPORTS", "dst": "m:f"})
    report = validate_graph(graph_to_rdf(g))
    assert any("IMPORTS" in v["rule"] for v in report.violations), report.describe()

def test_relation_between_unknown_nodes_is_reported():
    """端点不在节点集合中的关系必须报出, 不允许静默。"""
    g = valid_neo4j_graph()
    g["relations"].append({"src": "m:f", "rel": "CALLS", "dst": "does_not_exist"})
    report = validate_graph(graph_to_rdf(g))
    assert report.violations, report.describe()

def test_duplicate_uid_nodes_are_reported():
    """同一 uid 被声明两次 (MERGE 语义被破坏) 应报出。

    注意: 导出时 uid 相同的节点会塌缩成同一个 URI, 因此校验器必须
    在**导出前**统计重复声明, 而不是指望 RDF 层保留两个节点。
    """
    g = valid_neo4j_graph()
    g["entities"].append(
        {"type": "Class", "uid": "m:Repo", "name": "Repo", "module": "m",
         "filepath": "m.py", "lineno": 3, "external": False, "kind": "class"}
    )
    report = validate_graph(graph_to_rdf(g), entities=g["entities"])
    assert any("uid" in v["rule"].lower() for v in report.violations), report.describe()

def test_sparql_uses_subclass_reasoning():
    """rdfs:subClassOf* 应能同时取到 Method 与 Function。"""
    g = graph_to_rdf(valid_neo4j_graph())
    query = """
    PREFIX : <%s>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT (COUNT(?s) AS ?n) WHERE { ?s rdfs:subClassOf* :Symbol . }
    """ % str(NS)
    rows = list(g.query(query, initNs={"rdfs": NS.rdfs}))
    assert int(rows[0][0]) >= len(CLASS_LABELS)

def test_fixture_graph_validates_clean():
    """真实 fixture 图上违规数必须为 0 (或等于已登记缺口)。"""
    from benchmarks.runner import build_graph

    fixture = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "webapp_py"
    graph = build_graph(fixture)
    rdf = graph_to_rdf(
        {"entities": graph["entities"], "relations": graph["relations"]}
    )
    report = validate_graph(rdf)
    assert report.violations == [], report.describe()

def test_schema_ttl_matches_build_schema():
    """schema.ttl 与 build_schema() 必须一致, 否则文件会悄悄漂移。"""
    from ontology.schema import SCHEMA_PATH, build_schema, load_schema

    assert SCHEMA_PATH.exists(), "schema.ttl 缺失"
    assert set(load_schema()) == set(build_schema())
