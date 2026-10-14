# Constraints

Last reviewed: 2026-10-14 by @codex (autonomous run — 见文末说明)

本文件是本仓库的质量契约。**不得为了让某个改动通过而放宽本文件。**

## Floor (始终执行, 无需配置)

- 不新增抑制注释: `# noqa`、`# type: ignore`、`# fmt: skip`
- 不留未实现桩: `raise NotImplementedError`、空 `except: pass`
- 不跳过或删除测试; 若必须跳过, 提交信息里写明理由
- 不在源码中提交密钥 (`.env` 已在 `.gitignore`)
- 不靠改分母/改断言让 benchmark 变好看

## Enforced with numbers

| 维度 | 规则 | 检查命令 | 运行时机 |
|---|---|---|---|
| 图完整性 | fixture 图悬空边 = 0 | `uv run pytest tests/test_benchmark_runner.py -q` | 每次改动后 |
| 检索质量 | fixture macro recall = 1.000 (12 场景全满分) | `uv run python -m benchmarks.runner` | 每次改动后 |
| 单元测试 | 全部通过 | `uv run pytest tests/ -q` | 任务结束 |
| 本体约束 | fixture 违规数 = 已登记缺口数 | `uv run python -m ontology.validate` | 任务结束 |
| 指标口径 | 检索失败的 run 不得计入幻觉率均值 | `uv run pytest tests/test_observability_report.py -q` | 每次改动后 |
| 驱动复用 | 进程内只有一个 Neo4j driver | `uv run pytest tests/test_graph_store.py -q` | 每次改动后 |
| CLI 契约 | 失败退出码非零 + stderr 可读; `--json` 输出纯净 | `uv run pytest tests/test_cli.py -q` | 每次改动后 |

**本体约束的可判定性必须保持**: 注入 domain/range 违规、悬空边、重复 uid 三类缺陷时,
`ontology.validate` 必须报出对应规则 (由 `tests/test_ontology.py` 守护)。
否则该约束只是在读一份没人用的 TTL。

每一行都点名了产出判定的命令。没有命令的维度只是愿望, 不是约束。

## Measured, not yet enforced

| 指标 | 当前值 | 方向 |
|---|---|---|
| 单元测试数 | 188 (2026-10-14, 真实仓库评测后) | 只增不减 |
| fixture macro recall | 1.000 (12 计分场景, 全满分) | 只增不减 |
| fixture 悬空边 | 0 | 保持 0 |
| 本体违规数 | 0 (244 triples) | 保持 0 |

**可观测性基线 (2026-10-14 建立):** fixture 图 + 假回答端到端 6 runs,
幻觉率 50% (grounded 回答 33%, 掺入虚构符号的回答 67%), 检索失败 0 计分。
该基数的意义是**口径已验证**: grounded 回答的 33% 来自 `AuthService.login`
(传递调用者, 图检索不到 —— 这是 recall 的真实缺口, 不是指标 bug)。

**真实 LLM 基线 (2026-10-14, n=30):** 幻觉率均值 **3.6%** (中位数 0%),
grounding recall **100%**。该数字口径演进过三轮, 每次都是指标缺陷而非
模型编造: 81.8% (未归一化) -> 40.4% (30 次基线, 抽取假阳性) -> 3.6%
(逐条审计 105 个 flagged 符号后修复)。残留 4 项经人工确认为假设举例、
模块前缀与检索粒度, **真幻觉 0**。

**幻觉率口径约束**: 任何修改 `extract_symbols` 或 `compute_metrics` 的 PR,
若使该基线数字发生变化, 必须重新逐条审计 flagged 符号并说明分类依据 ——
**不得仅凭数字下降就宣称模型变好了**。

**样本量门槛**: 计分样本 < 30 时报告必须输出"不得对外宣称"警告
(符号抽取是下界, 见 SPEC-observability.md Open Questions)。

**本体层基线 (2026-10-14 建立):** fixture 图上违规数 = **0**, 244 triples。
已验证可检出 4 类真实缺陷: domain/range 违规 (label 串号)、悬空边、
重复 uid、BELONGS_TO 主体错误。

## Exceptions

当前无。新增例外需同时登记: 规则、路径、原因、责任人、过期时间。

## 关于本次落地的说明 (诚实记录)

`constraint-driven-development` 技能要求访谈真人后再定约束。本次为自主运行
 (用户指示"自己完成后续所有工作"), 无真人可访, 因此:

- 采用了该技能给出的**默认值** (Q2 block / Q3 测量并守住 / Q4 90 秒)
- 数字全部来自**实测**, 非编造: 测试数与 recall 均为本次运行输出
- 未选择需要运行中 URL 的维度 (Lighthouse/axe) —— 本项目无页面可测,
  该技能明确说这种情况应"说明并放弃该维度"而非发明一个跑不起来的检查

待人工复核的点: 90 秒检查预算 (当前 `pytest tests/` 约 35 秒, 已回到预算内)。
此前超支到 126 秒的原因是 live_db / live_store 用例真的连本地 Neo4j, 且连不上时
驱动的指数退避重试占了大头。已按 AGENTS.md「测试必须离线」改为假 store / 假 driver,
覆盖的分支没有减少 (137 -> 148 个测试)。
