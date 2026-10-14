# ADR 0001: 用 module:qualname 形式的 uid 作为节点唯一标识

- 状态: 已接受 (2026-10-13)
- 影响模块: `graph-core`

## Context

旧实现用裸 `name` 作为节点键: `MERGE (e:Function {name: $name})`。这导致:

1. 同名实体跨文件互相覆盖 —— `pkg/a.py` 与 `pkg/b.py` 的 `save()` 只能存一个,
   入库后图凭空少节点, 且无任何报错。
2. 关系匹配 `MATCH (src {name: $src})` 不限定 label, `:Function` 与 `:Class` 同名时互相串。

## Decision

节点唯一键改为 `uid = f"{module}:{qualname}"`, 例如 `auth.tokens:refresh_token`、
`auth.service:AuthService.login`。`name` 保留为属性, 供展示与模糊搜索。

跨文件解析由两阶段 `resolve_graph()` 完成: 先按 uid 归并节点, 再判定每条
跨模块边的目标是否存在; 不存在时创建 `:External` 桩, 保证边不悬空。

## Alternatives Considered

| 方案 | 否决原因 |
|---|---|
| 继续用 name, 加 label 到 key 里 | 只解决问题 2, 不解决跨文件同名覆盖 |
| 用 (filepath, lineno) 做 key | 行号随编辑漂移, 不适合做稳定标识; 且无法表达跨文件调用目标 |
| 用文件内容 hash | 同上, 且每次编辑都换 key, 图无法增量更新 |

## Consequences

**收益:**
- 同名实体不再互相覆盖, 跨模块 `IMPORTS`/`CALLS` 可解析 (实测 21 + 12 条)。
- 关系端点可校验: `GraphLoader` 写入前检查端点存在, 悬空边直接报错而非静默丢失。

**代价:**
- `app.py` 可视化必须改用 `uid` 作节点键 (name 不再唯一) —— 已同步修改。
- 查询侧需接受 `module:name` 形式; `agent.py` 的 `query_dependencies` 已支持,
  并在命中多个实体时明确报错而非静默取第一条。
- 图结构变化是**破坏性的**: 旧的按 name 查询的 Cypher 全部失效。
