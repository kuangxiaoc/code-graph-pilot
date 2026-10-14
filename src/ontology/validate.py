"""SHACL 约束校验 + 违规报告。

实现为显式规则求值而非完整 SHACL 引擎: 规则集小且固定, 自建求值器可读性更好,
且不引入 pyshacl 的解析开销。每条规则独立, 可单独测试。
"""

from rdflib import RDF, RDFS

from .schema import NS, REL_SPEC


class ViolationReport:
    """违规报告。violations 为 dict 列表, 便于聚合与断言。"""

    def __init__(self):
        self.violations = []

    def add(self, rule, node, detail):
        self.violations.append({"rule": rule, "node": str(node), "detail": detail})

    @property
    def count(self):
        return len(self.violations)

    def by_rule(self):
        out = {}
        for v in self.violations:
            out[v["rule"]] = out.get(v["rule"], 0) + 1
        return out

    def describe(self):
        if not self.violations:
            return "无违规"
        lines = [f"检出 {self.count} 条违规:"]
        for v in self.violations[:20]:
            lines.append(f"  [{v['rule']}] {v['node']}: {v['detail']}")
        if self.count > 20:
            lines.append(f"  ... 另有 {self.count - 20} 条")
        return "\n".join(lines)

    def to_dict(self):
        return {"count": self.count, "by_rule": self.by_rule(),
                "violations": self.violations}


def _type_of(graph, node):
    """取节点最具体的类型 (排除 :Symbol 这一抽象父类)。"""
    for t in graph.objects(node, RDF.type):
        if t != NS.Symbol and str(t).startswith(str(NS)):
            return t
    return None


def _subclasses(graph, cls):
    return set(graph.subjects(RDFS.subClassOf, cls))


def validate_graph(graph, entities=None):
    """对图执行全部约束, 返回 ViolationReport。

    entities: 原始节点列表。uid 重复检查必须在导出前做 —— RDF 层会把
    uid 相同的节点塌缩成同一个 URI, 重复信息随之丢失。
    """
    report = ViolationReport()

    # 规则 1: uid 重复声明 (MERGE 语义被破坏的信号)
    if entities is None:
        entities = []
    uid_counts = {}
    for entity in entities:
        uid_counts.setdefault(entity["uid"], []).append(entity)
    for uid, nodes in uid_counts.items():
        if len(nodes) > 1:
            report.add("shacl-duplicate-uid", uid, f"被声明 {len(nodes)} 次")

    _validate_rdf(graph, report)
    return report


def _validate_rdf(graph, report):
    """在 RDF 图上执行结构与类型约束。"""

    types = {}
    for node in set(graph.subjects(RDF.type, None)):
        t = _type_of(graph, node)
        if t is not None:
            types[node] = t
    all_nodes = set(types)

    # 规则 2: domain/range 约束
    for rel, (domain_name, range_name) in REL_SPEC.items():
        prop = NS[rel]
        domain = NS[domain_name]
        rng = NS[range_name]
        allowed_subjects = _subclasses(graph, domain) | {domain}
        allowed_objects = _subclasses(graph, rng) | {rng}

        for subj, obj in graph.subject_objects(prop):
            subj_type = types.get(subj)
            if subj_type is not None and subj_type not in allowed_subjects:
                report.add(
                    f"domain-{rel}", subj,
                    f"类型 {subj_type.split('#')[-1]} 不是 {domain_name} 的子类",
                )
            if obj not in all_nodes:
                report.add(f"range-{rel}", subj, f"目标 {obj} 不存在于节点集合")
            else:
                obj_type = types.get(obj)
                if obj_type is not None and obj_type not in allowed_objects:
                    report.add(
                        f"range-{rel}", subj,
                        f"目标类型 {obj_type.split('#')[-1]} 不是 {range_name} 的子类",
                    )

    # 规则 3: 关系主体必须是已声明节点 (悬空源端)
    for rel in REL_SPEC:
        for subj, _ in graph.subject_objects(NS[rel]):
            if subj not in all_nodes:
                report.add("dangling-source", subj, f"{rel} 的主体未声明")

    return report
