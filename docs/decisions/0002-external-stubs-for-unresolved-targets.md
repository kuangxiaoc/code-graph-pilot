# ADR 0002: 无法解析的调用目标创建 :External 桩, 不伪造 CALLS 边

- 状态: 已接受 (2026-10-13)
- 影响模块: `graph-core`

## Context

`obj.method()` 无法确定对象类型。旧实现把方法名当函数名建 `CALLS` 边:
`Repo.save()` 与 `Cache.save()` 全串到同一个 `save`。这产生两类问题:

1. **假边**: 影响分析把无关模块算进 blast radius。
2. **悬空边**: 目标节点根本不存在时, Neo4j 的 `MATCH` 静默丢弃该边 ——
   实测旧实现在自身 `src/` 上宣称 175 条关系, 其中 **113 条 (64%)** 端点不存在,
   实际落库仅 62 条, 且无任何告警。

## Decision

分三种情况处理, 且**每种都可观测**:

| 情况 | 处理 |
|---|---|
| 本模块内可解析 | 正常建 `CALLS` 边 |
| 目标在本批其他文件中 | `resolve_graph` 连通为真实边 |
| 目标不在本批 (外部库 / 未提供源码) | 建 `:External` 桩节点 + 边, `external=true` |
| 无法确定目标 (`obj.method()`) | 记入 `analyzer.unresolved_calls`, **不建边** |

另外 `GraphLoader.load_data()` 在写入前校验 label / 关系类型白名单与端点存在性,
任何一项不满足直接抛错。

## Alternatives Considered

| 方案 | 否决原因 |
|---|---|
| 按方法名建边 (旧行为) | 产生假边, 直接影响 blast radius 正确性 |
| 静默跳过悬空边 (旧行为) | 64% 的边无声消失, 图的质量无法评估 |
| 用占位符节点但无 `external` 标记 | 无法区分"真实外部依赖"与"解析失败", 报告会误导 |

## Consequences

**收益:**
- 图里每条边都指向真实节点, 悬空边恒为 0 (有测试守护)。
- "我们不知道"变成显式数据: 项目自身上报 75 处未解析调用, 可被 benchmark 统计。

**代价:**
- 边数比旧实现少 (旧 175 条含 113 条假/悬空, 新 136 条全部有效) ——
  用 recall 衡量时数字会下降, 但这是**诚实的下降**。
- 需要维护 `unresolved_calls` 的消费方 (benchmark 已消费)。

## 未解决的问题

局部变量类型推断尚未实现: `repo = Repository(...)` 之后的 `repo.save()` 无法解析。
这是 benchmark 场景 11 (0.545) 与 12 (0.800) 的共同根因, 已在
`benchmarks/README.md` 公开。
