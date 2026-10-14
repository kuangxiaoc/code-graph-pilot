"""RDFS/OWL 本体定义。

类层级:
    :Symbol
      |-- :Module
      |-- :Class
      |-- :Function
      |-- :Method
      +-- :External

每条关系都声明 domain/range, 使结构违规可被 SHACL 判定 —— 这正是旧实现
`MATCH (src {name: $src})` 不限定 label 导致同名 Class/Function 互串
这类缺陷的形式化描述。
"""

from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef

NS = Namespace("https://codegraph-pilot.dev/ontology#")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
SH = Namespace("http://www.w3.org/ns/shacl#")
OWL = Namespace("http://www.w3.org/2002/07/owl#")

# 节点 label 白名单, 与 graph_builder.NODE_LABELS 一致
CLASS_LABELS = ["Module", "Class", "Function", "Method", "External"]

# 关系类型 -> (domain, range)
REL_SPEC = {
    "CALLS": ("Symbol", "Symbol"),
    "IMPORTS": ("Module", "Module"),
    "INHERITS": ("Class", "Class"),
    "BELONGS_TO": ("Method", "Class"),
    "DEFINED_IN": ("Symbol", "Module"),
}

REL_TYPES = list(REL_SPEC)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.ttl"


def build_schema():
    """在内存中构造本体图 (类层级 + domain/range)。"""
    g = Graph()
    g.bind("", NS)
    g.bind("rdfs", RDFS)
    g.bind("sh", SH)
    g.bind("owl", OWL)

    g.add((NS.Symbol, RDFS.type, OWL.Class))
    for label in CLASS_LABELS:
        cls = NS[label]
        g.add((cls, RDFS.type, OWL.Class))
        g.add((cls, RDFS.subClassOf, NS.Symbol))
        g.add((cls, RDFS.label, Literal(label)))

    for rel, (domain, rng) in REL_SPEC.items():
        prop = NS[rel]
        g.add((prop, RDFS.type, OWL.ObjectProperty))
        g.add((prop, RDFS.domain, NS[domain]))
        g.add((prop, RDFS.range, NS[rng]))
        g.add((prop, RDFS.label, Literal(rel)))

    return g


def load_schema():
    """加载本体: 优先读 schema.ttl, 不存在则现场构造 (保证可离线运行)。"""
    if SCHEMA_PATH.exists():
        g = Graph()
        g.parse(SCHEMA_PATH, format="turtle")
        return g
    return build_schema()
"""RDFS/OWL 本体定义。

类层级:
    :Symbol
      |-- :Module
      |     +-- :External     (外部库/未解析符号桩, 也是 IMPORTS 的合法 target)
      |-- :Class
      |-- :Function
      |-- :Method

每条关系都声明 domain/range, 使结构违规可被 SHACL 判定 —— 这正是旧实现
`MATCH (src {name: $src})` 不限定 label 导致同名 Class/Function 互串
这类缺陷的形式化描述。
"""

from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef

NS = Namespace("https://codegraph-pilot.dev/ontology#")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
SH = Namespace("http://www.w3.org/ns/shacl#")
OWL = Namespace("http://www.w3.org/2002/07/owl#")

# 节点 label 白名单, 与 graph_builder.NODE_LABELS 一致
CLASS_LABELS = ["Module", "Class", "Function", "Method", "External"]

# 关系类型 -> (domain, range)
REL_SPEC = {
    "CALLS": ("Symbol", "Symbol"),
    "IMPORTS": ("Module", "Module"),
    "INHERITS": ("Class", "Class"),
    "BELONGS_TO": ("Method", "Class"),
    "DEFINED_IN": ("Symbol", "Module"),
}

REL_TYPES = list(REL_SPEC)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.ttl"


def build_schema():
    """在内存中构造本体图 (类层级 + domain/range)。"""
    g = Graph()
    g.bind("", NS)
    g.bind("rdfs", RDFS)
    g.bind("sh", SH)
    g.bind("owl", OWL)

    g.add((NS.Symbol, RDFS.type, OWL.Class))
    for label in CLASS_LABELS:
        cls = NS[label]
        g.add((cls, RDFS.type, OWL.Class))
        g.add((cls, RDFS.subClassOf, NS.Symbol))
        g.add((cls, RDFS.label, Literal(label)))

    # :External 是 :Module 的子类: 外部模块桩同样是 IMPORTS 的合法 target,
    # 否则 `m IMPORTS os` 会被判为 range 违规 (真实图中这是合法结构)。
    g.add((NS.External, RDFS.subClassOf, NS.Module))
    # 外部符号桩 (os:getenv 这类) 也是 Symbol 的合法子类, 上面已声明。

    for rel, (domain, rng) in REL_SPEC.items():
        prop = NS[rel]
        g.add((prop, RDFS.type, OWL.ObjectProperty))
        g.add((prop, RDFS.domain, NS[domain]))
        g.add((prop, RDFS.range, NS[rng]))
        g.add((prop, RDFS.label, Literal(rel)))

    return g


def load_schema():
    """加载本体: 优先读 schema.ttl, 不存在则现场构造 (保证可离线运行)。"""
    if SCHEMA_PATH.exists():
        g = Graph()
        g.parse(SCHEMA_PATH, format="turtle")
        return g
    return build_schema()
