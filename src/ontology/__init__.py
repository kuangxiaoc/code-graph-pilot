"""本体层: RDFS/OWL schema + SHACL 约束校验。

分层原则: schema 与约束走 RDF/OWL, 运行时图遍历走 Cypher。
详见 docs/specs/SPEC-ontology.md 与 docs/decisions/0004。
"""
