# ADR 0003: Benchmark 打分为 recall, 不测 accuracy

- 状态: 已接受 (2026-10-14)
- 影响模块: `eval-harness`

## Context

需要一个指标衡量检索质量。候选口径有 accuracy / precision / recall / F1。

本项目的核心场景是**影响分析**: "改这个函数会波及谁"。在这个场景里:

- **漏召回** = 漏掉一个真实调用点 = 改坏线上代码, 代价极高。
- **多召回** = 多报一个可疑点 = 开发者多看一眼, 代价极低。

两类错误代价严重不对称, 因此 accuracy / F1 这类对称口径不适用。

## Decision

打分口径为 **recall**:

```
recall = |expected ∩ predicted| / |expected|
```

ground truth 人工从源码得出, 不来自工具输出。

配套规则:

1. **`known_gap` 场景单独统计, 不计入总分**, 但缺陷必须在 `_meta.known_gaps`
   与 README 中公开。
2. **允许工具拿不到满分**。cartog 的 benchmarks README 原话:
   *"Some scenarios deliberately keep ground truth at the objectively correct answer
   read from source, so cartog scores below 100% where its resolution is incomplete."*
3. 每个 fixture 用独立的图实例, 不共享状态 —— 否则 recall 会测到
   "最后索引的那个" fixture (cartog 踩过的坑)。

## Alternatives Considered

| 方案 | 否决原因 |
|---|---|
| accuracy | 掩盖漏召回, 而漏召回是本项目最危险的错误 |
| precision | 反过来, 会诱导工具变保守, 少报 |
| F1 | 对称加权, 同样不反映代价不对称 |

## Consequences

**收益:**
- 指标方向与业务代价一致。
- 基线诚实: 当前 0.945, 两个未满分场景的根因被明确记录而非藏起来。

**代价:**
- 该指标**不能**证明精度。若将来要向用户宣称"结果干净", 需另测 precision。
  ContextBench 同时报 coverage 与 precision 正是这个原因。
- 单场景 recall 可能虚高 (expected 集合小时), 因此汇总用 macro-average 而非 micro。

## 当前基线

```
fixture: webapp_py
graph: 26 symbols, 67 relations, 0 dangling edges
macro recall: 0.945   (12 计分场景, 10 满分, 1 known_gap)
```

`CONSTRAINTS.md` 已把 `macro recall >= 0.94` 设为不可倒退线。
