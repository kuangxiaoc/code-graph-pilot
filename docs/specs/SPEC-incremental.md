# Spec: incremental (增量更新)

**Module id:** `incremental` (修硬伤 #5)

## Objective

当前每次入库都 `DETACH DELETE` 全库 (除 `--keep`)。`--keep` 名义上是增量,
**实际有已证明的缺陷**: 源码里重命名/删除的符号会残留在图里, 连同它的 CALLS 边。

实测证据 (2026-10-14):
```
1. build /tmp/inc_test           -> 图里有 pkg.caller:entry, 及其 CALLS 边
2. 源码中 entry 改名为 renamed_entry
3. build /tmp/inc_test --keep    -> 图里同时有 entry 和 renamed_entry
                                  entry 的 CALLS 边仍然存在 (残留 1 条)
```

残留边的后果: 影响分析会报告一个**已不存在的调用者**, 直接违反"确定性分析"
这个卖点 —— 比全库重建更糟, 因为它看起来是增量的。

## 方案: 按文件替换子图, 不做内容 hash

```
对每个变更文件 f:
  1. 删除 f 的 Module 节点及其 DEFINED_IN 全部符号 (DETACH DELETE)
  2. 重新解析 f, 写回
  3. resolve_graph 重跑 (跨文件调用需要全量定义才能解析)
```

**为什么不做内容 hash 做 dirty 检测**: 那需要维护一张 (file -> hash) 状态表,
而跨文件解析本来就需要全量 analyzers。当"必须全量解析"时, 省下的只是
写库部分; 而写库是 O(变更文件)。加一张状态表的复杂度 > 收益。
真要做 hash 优化, 应该先做"解析结果缓存", 那是另一个 spec。

**模块粒度而非仓库粒度**: 一个文件对应一个 `:Module` 节点 + 该文件的符号,
`DEFINED_IN` 让归属可查, 因此删除边界清晰。

## 已知不清除的残留

只删"本文件定义的"子图。**别的文件指向本文件符号的 CALLS 边**由第 3 步
resolve_graph 重建时覆盖, 但如果某文件本次未解析, 它的旧边仍指向已删除符号。
→ 这是"部分增量"的固有代价, 处理方式是 CLI 提示: `--keep` 模式下如果只传
了部分文件, 输出里明确标注可能残留。

更彻底的做法是每次都对同一批文件全量解析 (即"批内一致"), 这也是默认行为:
`build <dir>` 传目录时, 目录内所有文件一起解析, 批内一致, 无残留。

## Boundaries

- **Always**: 删除子图前必须确认 uid 前缀属于该 Module, 不得按 name 删
  (同名跨模块会误删)。
- **Never**: 在 `build <dir>` (默认, 无 --keep) 之外保留旧的 DETACH DELETE 全库路径
  以外的语义 —— 默认仍是全库重建, 因为那是最安全的。
- **Ask first**: 引入 file hash 缓存 (会改变"何时需要重新解析"的语义)。

## Success Criteria

1. `build <dir> --keep` 后, 源码中已删除/重命名的符号不再出现在图里。
2. 残留 CALLS 边数为 0 (有测试守护, 直接复现实测证据那一幕)。
3. 默认 `build <dir>` 行为不变 (全库重建), 现有测试全绿。
