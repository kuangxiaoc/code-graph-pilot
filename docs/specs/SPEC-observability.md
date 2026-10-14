# Spec: observability (Agent 轨迹与幻觉率)

**Module id:** `observability` (见 `capability-map.md`)

## Objective

把 Agent 工作流从"不可观测的黑盒"变成可度量系统, 量化三个当前完全未知的问题:
幻觉率、覆盖率、修正有效率。

**这是本项目独有的差异化指标。** 竞品 (codegraph/cartog/rgctl/grafel) 全是无 LLM
生成环节的检索引擎, 结构上不存在"幻觉"这个概念, 也不会去测它。

## 先定义问题, 再埋点 (observability 技能步骤 1)

```
FEATURE: LangGraph 影响分析工作流 (parse -> retrieve -> generate -> review)

QUESTIONS ON-CALL WILL ASK:
1. 回答里提到的符号, 有多少是图里根本没有的?        -> 幻觉率
2. 图检索到的关键事实, 回答漏掉了多少?              -> 覆盖率
3. review 节点跑完之后, 幻觉率真的降了吗?          -> 修正有效率
4. 一次 run 是 PASS 结束的还是 max_revision 强停的? -> terminated_by 分布

-> 每个信号都必须回答上面至少一个问题, 否则不埋。
```

## 指标定义 (纯集合运算, 不引入 LLM judge)

设 `retrieved` = 图谱检索命中的符号集合, `claimed` = 从回答文本中抽取的符号集合。

```
hallucinated       = claimed - retrieved
hallucination_rate = |hallucinated| / |claimed|
grounding_recall   = |claimed ∩ retrieved| / |retrieved|
```

**为什么不用 LLM judge:** 判定"图里没有的东西出现在答案里"是纯集合差运算,
可重复、零成本、无模型偏差。LLM judge 只在需要判断"表述是否准确"时才值得引入。

符号抽取规则 (从中文 markdown 中提取标识符):
- 反引号片段优先 (回答中被 `code` 标注的最可信)
- `snake_case`、`CamelCase`、点分限定名 (`module:qualname` 中的 qualname 部分)

## Tech Stack

无新增依赖。轨迹用 JSONL 落盘 (`traces/`, gitignored), 分析用 stdlib。
理由: 样本量小 (单次运行), 不需要时序数据库; JSONL 可用 `jq` 直接查。

## Commands

```bash
uv run python -m observability.report            # 汇总全部轨迹
uv run python -m observability.report --last 20  # 只看最近 20 条
uv run pytest tests/test_observability.py -q
```

## Project Structure

```
src/observability/
  trace.py       # 轨迹记录 + 符号抽取 + 指标计算
  report.py      # JSONL -> 汇总报告
  __main__.py    # CLI
traces/          # JSONL, gitignored
tests/test_observability.py
```

## Boundaries

- **Always**: 埋点必须能关掉 (`CODEGRAPH_TRACE=0` 禁用), 不得拖慢正常路径。
- **Never**: 把用户 query 原文写入轨迹文件之外的地方; 不得记录 API Key。
- **Ask first**: 新增轨迹字段 (会改变报告口径)。

## Success Criteria (可验收)

1. `trace.py` 提供 `record_trace()` 与 `compute_metrics()`, 指标定义与上文一致。
2. `agent.py` 的 4 个节点接入埋点, 记录 `nodes_visited` / `revision_count` /
   `review_verdicts` / `retrieved_symbols` / `claimed_symbols` / `terminated_by`。
3. `terminated_by` 能区分 `pass` 与 `max_revision` —— 旧实现里二者在最终输出上
   无法区分 (`check_review_outcome` 的 `revision_count >= 1` 会让第二次 FAIL 也无条件放行)。
4. `report.py` 输出幻觉率、覆盖率、修正有效率三个数字 + 样本量。
5. `tests/test_observability.py` 全绿且离线 (不调 LLM: 用假 response 文本)。
6. 用 fixture 图 + 假回答跑通一次端到端, 产出真实数字。

## Open Questions

- **符号抽取的召回**: 中文回答里可能用"这个函数"指代而未写出符号名。
  这类漏抽会**低估**幻觉率 (漏抽的既不在 retrieved 也不在 claimed)。
  处理: 报告里明确标注该指标是下界, 并在样本量 < 30 时不对外宣称。
- **是否修 `check_review_outcome`**: 修不修都会改变 `terminated_by` 分布的口径。
  决定: **先只埋点不修**, 让基线反映真实行为; 修不修等看到数字再定。
  这正是本模块的价值 —— 用数据决定架构, 而不是因为 prompt 里写着 "Critic"。
