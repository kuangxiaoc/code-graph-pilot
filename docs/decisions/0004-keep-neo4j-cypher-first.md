# ADR 0004: 保留 Neo4j 与 Cypher, 不迁 SQLite

- 状态: 已接受 (2026-10-14, 取代 2026-10-13 的错误建议)
- 影响模块: `graph-core`, `delivery-surface`

## Context

调研竞品 (codegraph 73k star / cartog / rgctl / grafel) 发现**全部使用 SQLite 或
单文件二进制图**, 零个用图数据库。cartog 原话: *"One static binary, one SQLite file.
No Python, no pip, no Docker, no cloud."*

2026-10-13 的 `docs/knowledge/01` 据此建议本项目把默认存储换成 SQLite。

**该建议已被推翻**, 原因是过度外推:

| | 竞品 | 本项目 |
|---|---|---|
| 形态 | 被别人的 agent 调用的 **MCP 工具** | 自带编排与 UI 的 **Agent 应用** |
| 分发约束 | 必须 `curl \| sh` 装到开发者机器, 零依赖是硬需求 | 用户已接受 `docker-compose up` |
| 查询模式 | 单次符号查询 | 变长路径 + 双向遍历 + 多跳推理 |

竞品的零依赖约束来自"要分发到别人的机器上", 本项目没有这个约束。

## Decision

1. **Neo4j 继续作为默认后端**, Cypher 作为一等查询能力保留并强化。
2. 引入 `GraphStore` 协议, 目的是**可测性 + 解耦**, 以及为将来 MCP 分发预留
   轻量后端的位置 —— **不是**现在换库。
3. SQLite 后端仅在"明确要做 MCP 单文件分发"成为目标时才实现。

## Alternatives Considered

| 方案 | 否决原因 |
|---|---|
| 换成 SQLite 默认 (2026-10-13 的建议) | 自断一臂: Cypher 变长路径是本项目"确定性分析"卖点的来源 |
| 完全不上抽象, 直连 driver | 图算法无法离线测试; 且每次查询新建 driver (硬伤 #8) |
| 同时维护两个对等后端 | 无消费方驱动, 纯属浪费 |

## Consequences

**收益:**
- 保留核心能力:
  ```cypher
  MATCH (a:Function {name: $name})-[:CALLS*1..5]->(b) RETURN DISTINCT b
  ```
  一行完成 5 跳传递闭包; SQLite 需写递归 CTE 或手写 BFS。
- Neo4j Browser 白送可视化, 无需自建。

**代价:**
- 使用者必须先起 Docker, 上手摩擦高于竞品。缓解: `docker-compose.yml` 已一键启动。
- 将来做 MCP 分发时需补 SQLite 后端 (届时按需)。

## 相关

本体层 (见 `docs/specs/SPEC-ontology.md`) 同样**不迁移图数据库**: RDF/OWL 只承担
schema 与约束校验, 运行时遍历仍走 Cypher。
