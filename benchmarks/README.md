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
graph: 26 symbols, 72 relations, 0 dangling edges
macro recall: 1.000   (13 个计分场景全部满分, 0 known_gap)
```

## 曾经的缺口 (已修复, 留档)

最初两个场景未满分, 根因是 **缺少局部变量类型推断**:

```python
repo = Repository(connect("db://main"))
repo.save(payload)          # 当时解析不到 services.db:Repository.save
```

当时的处置是记入 `unresolved_calls` 而不伪造边 (刻意: 宁缺勿假),
随后补齐了解析 —— 现在 `obj.method()` 支持四种可靠来源:
构造函数 (`repo = Repository()`)、链式赋值、参数类型标注
(`def f(r: Response)`)、方法返回值标注 (`conn = self.get_conn()`,
含沿继承链向上查)。推断不出时**仍然**只记 unresolved, 不猜。
场景 11 因此从 0.545 到 1.000。

另一条 known_gap 是场景 13: `self.audit()` 的 `audit` 继承自
`BaseService`, 当时不做继承感知的方法解析。已支持沿继承链查找后,
该场景 recall 1.0, 于 2026-10-15 恢复计分 (同时补上当初漏标的
调用者 `routes.admin:admin_route`)。恢复计分的依据是**缺陷消失**,
不是因为改了口径。

剩余残差不在这个 fixture 上, 而在真实仓库的 `super().method()`
且父类来自外部库 —— 见 README「真实仓库评测」一节。

## 维护规则

1. **ground truth 必须人工从源码得出**, 不得复制工具输出 —— 否则测试只证明
   工具等于它自己。
2. 修改 fixture 后必须同步手改 `ground_truth/*.json`。
3. 已知缺陷用 `"status": "known_gap"` 标注并写进 `_meta.known_gaps`,
   不计入总分, 但要在文档里公开。
4. 每个 fixture 必须使用独立的图实例, 不得共享状态
   (否则 recall 会测到"最后索引的那个" fixture)。
