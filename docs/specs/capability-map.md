# Capability Map: Ontology-grounded Code Intelligence Agent

本仓库此前以 6 个提交推进一条终局路线, 但未留下能力边界说明。本文补上 Phase 0
能力地图, 作为后续所有 spec 的索引。

## 能力拆分

| Module id | 职责 | 依赖 | 状态 |
|---|---|---|---|
| `graph-core` | AST 解析、uid 图模型、`resolve_graph`、Neo4j 加载、增量索引 | — | 已交付 (macro recall 0.945) |
|| `eval-harness` | fixture、人工 ground truth、场景打分、回归基线 | `graph-core` | 已交付 |
| `ontology` | RDFS/OWL schema + SHACL 约束校验 + 违规报告 | `graph-core` | 未开始 |
| `agent-orchestration` | LangGraph 多节点、checkpoint、意图抽取 | `graph-core` | 部分 (4 节点, 无 checkpoint) |
| `observability` | 轨迹埋点、幻觉率、修正有效率 | `agent-orchestration` | 未开始 |
| `delivery-surface` | CLI / MCP / Streamlit UI 三入口 | `graph-core`, `agent-orchestration` | 部分 (仅 UI) |

## 依赖方向

```
graph-core ──┬──> eval-harness
             ├──> ontology
             └──> agent-orchestration ──> observability
             └──> delivery-surface
```

无环。`ontology` 与 `agent-orchestration` 互不依赖, 可并行。

## 构建顺序

```
1. graph-core            (已完成, 继续收尾: 局部变量类型推断、增量更新)
2. eval-harness          (已完成, 基线已固化)
3. ontology              ← 下一站: 用户点名要巩固的能力
4. observability         ← 独有差异化: 竞品无 LLM 生成环节, 不测幻觉率
5. delivery-surface      ← MCP/CLI, 最后做
```

## 判定 "独立可切" 的依据

按 Phase 0 检测规则逐条核对:

- **各有独立消费方**: `ontology` 的消费方是校验脚本与查询层; `observability` 的消费方是
  轨迹报告; 二者可以各自上线/各自下线而不影响对方验收。
- **验收标准各自成群**: `ontology` 的验收是"检出 N 条真实违规"; `observability` 的验收是
  "幻觉率数字 + `terminated_by` 分布"。
- **可整体替换**: `ontology` 换成 SHACL-only 实现不影响 `observability`; 反之亦然。

因此判定为**多能力打包**, 按上表分模块递归做 spec, 而非写一个大 spec。

## 明确不做 (YAGNI)

- 多智能体协作、长期记忆: 当前无证据表明 4 节点 LangGraph 已到瓶颈, 不加。
- 跨语言解析 (Java/Go): 需引入 tree-sitter, 与 Ontology 巩固目标正交。
- CFG/PDG / CPG 改名: 最贵且非用户目标, 继续用 CKG。
- 自建 Vector DB: README 已宣称的 Vector 部分是虚构的, 直接删除而非补实现。
