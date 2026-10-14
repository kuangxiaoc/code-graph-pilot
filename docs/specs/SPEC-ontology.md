# Spec: ontology (本体层)

**Module id:** `ontology` (见 `capability-map.md`)

## Objective

为代码图定义一层**可执行的语义约束**, 使图数据的结构缺陷能被自动检出,
而不是靠人工读 Cypher 发现。

用户目标是"巩固知识图谱/Ontology 这一块 (本体建模、实体关系抽取、语义约束、语义推理;
RDF/RDFS/OWL/SPARQL; Neo4j/GraphDB/Jena/RDFLib)"。本 spec 把该目标转成可验收条件。

**核心价值不是"用了语义网技术", 而是本体层能抓到真实违规。**
若本体层跑完只产出一份没人用的 TTL 文件, 本模块视为失败。

## Tech Stack

| 用途 | 选型 | 理由 |
|---|---|---|
| schema 与约束 | `rdflib` (RDFS/OWL) + SHACL 形状校验 | 属性图无内置推理; 约束校验正是语义网主场 |
| 运行时图遍历 | **保持 Neo4j + Cypher** | 变长路径是"确定性分析"的来源, 不迁移 |
| 带推理的查询 | SPARQL (rdflib) | 需要 `rdfs:subClassOf*` 传递闭包时用 |

**分层原则: schema/约束走 RDF+OWL, 图遍历走 Cypher。**
两套查询语言不是过度设计, 是各用其所长 (详见 `docs/knowledge/01` §1)。

## Commands

```bash
uv add rdflib py-shacls
uv run python -m ontology.export        # Neo4j -> RDF (ttl)
uv run python -m ontology.validate      # SHACL 校验, 输出违规报告
uv run pytest tests/test_ontology.py
```

## Project Structure

```
ontology/
  schema.ttl        # RDFS/OWL 本体: 类层级 + 属性 domain/range
  shapes.ttl        # SHACL 形状: 约束 (可判定的违规条件)
  export.py         # Neo4j 图 -> RDF triples
  validate.py       # SHACL 校验 + 违规报告
  report.py         # 违规汇总 (按规则聚合, 输出 markdown/json)
tests/test_ontology.py
```

## 本体设计

```turtle
:Module     rdfs:subClassOf :Symbol .
:Class      rdfs:subClassOf :Symbol .
:Function   rdfs:subClassOf :Symbol .
:Method     rdfs:subClassOf :Symbol .
:External   rdfs:subClassOf :Symbol .   # 外部库/未解析符号桩

:CALLS      rdfs:domain :Symbol ; rdfs:range :Symbol .
:IMPORTS    rdfs:domain :Module ; rdfs:range :Module .
:INHERITS   rdfs:domain :Class  ; rdfs:range :Class .   # 必须 Class->Class
:BELONGS_TO rdfs:domain :Method ; rdfs:range :Class .
:DEFINED_IN rdfs:domain :Symbol ; rdfs:range :Module .
```

## Code Style

沿用仓库约定: 4 空格缩进、`snake_case`、中文 docstring。本体文件 (`.ttl`) 内
类名 PascalCase、属性名 CamelCase, 注释用英文 (便于外部推理工具消费)。

## Testing Strategy

pytest, 位置 `tests/test_ontology.py`, 全部**离线**: 用 rdflib 内存图,
不连 Neo4j。三层:

1. **本体自身** (schema 正确性): domain/range 自洽、类层级无环。
2. **SHACL 规则** (约束可判定): 构造故意违规的图, 断言 `validate.py` 报出对应形状。
3. **真实图端到端**: 在 fixture 图上导出+校验, 断言违规数为 0 (或等于已登记缺口数)。

## Boundaries

- **Always**: 本体改动必须同步跑 `tests/test_ontology.py`; 新增约束必须配一个"故意违规"的测试。
- **Ask first**: 新增节点 label 或关系类型 (改的是图模型, 影响 `graph-core`)。
- **Never**: 为了让校验通过而放宽 SHACL 形状; 把违规数硬编码为 0 来让测试变绿。

## Success Criteria (可验收)

1. `ontology/schema.ttl` 定义上述 5 个 `rdfs:subClassOf` 与 5 组 `rdfs:domain`/`rdfs:range`。
2. `ontology/validate.py` 能在 fixture 图上跑完并产出违规报告 (markdown + json)。
3. **约束能抓到真实违规**: 构造一个刻意引入 label 串号的图 (同名 `:Function` 与 `:Class`
   被误连), 断言 SHACL 报出 domain/range 违规。
4. fixture 图上的违规数为 0, 或等于 `_meta.known_gaps` 中登记的缺口数。
5. 提供至少 1 条可运行的 SPARQL 查询, 展示推理能力
   (如 `rdfs:subClassOf*` 求 Method 与 Function 的并集)。
6. `tests/test_ontology.py` 全绿, 且不依赖 Neo4j 与网络。

## Open Questions

- **RDfLib 与 Neo4j 的映射粒度**: 是否将 Cypher 结果全部物化为 triples?
  倾向: 只导出 schema 相关的 5 类节点与 5 种关系, 不导出 `lineno`/`filepath` 等属性
  (它们与约束无关, 导出只会让三倍体量暴涨)。
- **推理机的取舍**: 用 rdflib 内置的 RDFS 闭包计算即可, 不引 Jena/Pellet。
  RDFS 推理对代码 schema 足够, OWL-DL 推理器在此规模上是净负担。

以上两条按倾向方案直接落地, 若与预期不符再调整。
