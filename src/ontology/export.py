"""把 Neo4j 图 (entities + relations) 导出为 RDF triples。

只导出与约束相关的结构: 5 类节点 + 5 种关系。不导出 lineno/filepath 等
展示属性 —— 它们与约束无关, 导出只会让三倍体量暴涨 (见 SPEC-ontology.md)。
"""

from rdflib import Graph, Literal, RDF, RDFS, URIRef

from .schema import NS, RDFS, build_schema


def _node_uri(uid):
    return NS["node/" + uid]


def graph_to_rdf(graph, schema=None):
    """把图转为 RDF Graph。

    graph: {"entities": [...], "relations": [...]}
    """
    g = build_schema() if schema is None else schema
    g.bind("node", NS.node) if hasattr(NS, "node") else None

    for entity in graph["entities"]:
        label = entity["type"]
        node = _node_uri(entity["uid"])
        # 类型用 rdf:type (RDF 标准), 不是 rdfs:type
        g.add((node, RDF.type, NS[label]))
        # uid 保留为字面量, 供 SHACL 检查重复声明
        g.add((node, NS.uid, Literal(entity["uid"])))
        g.add((node, NS.name, Literal(entity.get("name") or entity["uid"])))
        if entity.get("module"):
            g.add((node, NS.module, Literal(entity["module"])))
        if entity.get("external"):
            g.add((node, NS.external, Literal(True)))

    for rel in graph["relations"]:
        g.add(
            (
                _node_uri(rel["src"]),
                NS[rel["rel"]],
                _node_uri(rel["dst"]),
            )
        )

    return g
