# 本体层 (ontology)

为代码图提供**可执行的语义约束**: 图数据的结构缺陷被自动检出, 而不是靠人工读 Cypher 发现。

## 运行

```bash
uv run python -m ontology validate --fixture webapp_py   # 校验, 有违规则退出码 1
uv run python -m ontology schema                          # 打印本体 (turtle)
uv run pytest tests/test_ontology.py -q
```

## 分层原则

| 用途 | 技术 | 为什么 |
|---|---|---|
| schema + 约束校验 | RDFS/OWL + SHACL 形状规则 | 属性图无内置推理, 约束校验是语义网主场 |
| 运行时图遍历 | **Neo4j + Cypher** | 变长路径是"确定性分析"的来源, 不迁移 |
| 带推理的查询 | SPARQL (`rdfs:subClassOf*`) | 需要子类传递闭包时用 |

两套查询语言不是过度设计, 是各用其所长。决策记录见
`docs/decisions/0004-keep-neo4j-cypher-first.md`。

## 本体结构

```
:Symbol
  |-- :Module
  |     +-- :External     外部库/未解析模块桩
  |-- :Class
  |-- :Function
  |-- :Method

:CALLS      :Symbol -> :Symbol
:IMPORTS    :Module -> :Module
:INHERITS   :Class  -> :Class
:BELONGS_TO :Method -> :Class
:DEFINED_IN :Symbol -> :Module
```

`:External` 同时是 `:Module` 与 `:Symbol` 的子类 —— 外部模块桩是 `IMPORTS` 的
合法 target, 外部符号桩是 `CALLS` 的合法 target。少这一条会让 `m IMPORTS os`
被误判为 range 违规 (真实图中这是合法结构)。

## 已验证可检出的缺陷

以下 4 类均已在 fixture 图上**注入并实测报出**, 不是纸面规则:

| 注入的缺陷 | 报出规则 |
|---|---|
| `:Function` 参与 `INHERITS` (旧 `MATCH (src {name})` 不限定 label 的串号后果) | `domain-INHERITS` |
| CALLS 指向不存在的节点 (旧 `:REL_TYPE` 悬空边的同类根因) | `range-CALLS` |
| 同一 uid 声明两次 (MERGE 语义被破坏) | `shacl-duplicate-uid` |
| `BELONGS_TO` 的主体是 Function | `domain-BELONGS_TO` |

第 1 条正是本项目十条硬伤中 #3 的形式化描述: **label 串号在本体里就是一条
domain/range 约束被违反**, 可被机器判定。

## 当前基线

```
fixture: webapp_py
triples: 249   violations: 0
```

## 已知边界

- 校验器是**显式规则求值**, 非完整 SHACL 引擎: 规则集小且固定, 自建求值器可读性更好。
- 只导出 5 类节点 + 5 种关系。`lineno`/`filepath` 等展示属性不导出 ——
  它们与约束无关, 导出只会让三倍体量暴涨。
- 推理用 rdflib 内置 RDFS 闭包, 不引 Jena/Pellet: 对代码 schema 足够,
  OWL-DL 推理器在此规模上是净负担。

## 维护

- 改 `REL_SPEC` / 类层级必须同步重新生成 `schema.ttl`
  (`uv run python -m ontology schema > src/ontology/schema.ttl`),
  `test_schema_ttl_matches_build_schema` 会守护二者一致。
- **新增约束必须配一个"故意违规"的测试** (见 `tests/test_ontology.py`)。
- 不得为了让校验通过而放宽形状。
