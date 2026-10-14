# Benchmark Suite

度量代码图的检索质量: 在关系已知的 fixture 上, 对比预测结果与人工
ground truth, 报告 **recall**。

## 为什么测 recall 而不是 accuracy

检索系统漏召回比多召回危害大得多: 漏掉一个真实调用点意味着改坏线上代码,
多召回只是多读几行。因此打分口径是 recall, 允许高召回低精度。

## 运行

```bash
uv run python benchmarks/runner.py            # 打印表格
uv run python benchmarks/runner.py --json     # 机器可读输出
```

退出码 1 表示存在非满分场景, 可直接接进 CI。

## 目录

```
fixtures/webapp_py/     控制变量的 Python fixture (auth/service/routes/db/logger)
ground_truth/webapp_py.json   人工从源码得出的期望结果
runner.py               构图 + 打分
```

## 当前基线

```
graph: 26 symbols, 67 relations, 0 dangling edges
macro recall: 0.945   (12 个计分场景, 10 个满分, 1 个已知缺陷不计分)
```

## 已知缺口 (诚实披露)

两个场景未满分, 根因相同 —— **缺少局部变量类型推断**:

```python
repo = Repository(connect("db://main"))
repo.save(payload)          # 解析不到 services.db:Repository.save
```

`obj.method()` 无法确定对象类型时, 当前实现记入 `unresolved_calls`
而不伪造边 (这是刻意的: 宁缺勿假)。修复方向是解析赋值右侧的构造调用。
该缺口同时影响场景 11 (0.545) 与场景 12 (0.800)。

另有场景 13 (`self.audit()` 继承自基类) 标记为 `known_gap`, 不计分。

## 维护规则

1. **ground truth 必须人工从源码得出**, 不得复制工具输出 —— 否则测试只证明
   工具等于它自己。
2. 修改 fixture 后必须同步手改 `ground_truth/*.json`。
3. 已知缺陷用 `"status": "known_gap"` 标注并写进 `_meta.known_gaps`,
   不计入总分, 但要在文档里公开。
4. 每个 fixture 必须使用独立的图实例, 不得共享状态
   (否则 recall 会测到"最后索引的那个" fixture)。
