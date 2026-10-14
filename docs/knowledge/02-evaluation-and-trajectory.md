# 评测 / 校验 / 轨迹分析：把 "零量化" 补成可交付资产

> 对应硬伤 #10（无任何量化）与 #9（README 在吹）。
> 方法来源：cartog 的 `benchmarks/` 目录结构与 `contextbench/` 适配层一手代码，
> ContextBench (arXiv 2602.05892) 论文基准，以及 SWE-bench 系轨迹分析实践。

---

## 0. 核心原则：先定义 "对"，再测量 "准"

绝大多数 AI 项目的评测做错了顺序——先跑起来，再想怎么打分。正确顺序：

```
1. 固定 fixture（controlled repo，关系已知）
2. 手写 ground truth（人工标注，不来自自家工具输出）
3. 定义指标（coverage / precision / latency / tokens）
4. 跑自家工具 + 跑基线（grep）
5. 公布表格，含残差解释
```

cartog 的 `benchmarks/README.md` 里有一句值得原样抄进我们的文档：

> *"Some scenarios deliberately keep ground truth at the **objectively correct** answer
> read from source, so cartog scores below 100% where its resolution is incomplete.
> These rows exist to track that."*

**敢让自家工具在公开表格里拿不到 100%，是可信度的来源，不是污点。**

---

## 1. 第一层：Shell 层 benchmark（无 LLM，最便宜）

### 1.1 Fixture 设计

cartog 用 **同一个业务域的 10 种语言 fixture**（auth service / tokens / routes / middleware / db / cache / validators），
共 387 文件 ~19,760 LOC。这样设计的好处：

- **同域** → 跨语言可比（同一个 "who calls validate_token?" 在 10 种语言都有答案）
- **关系已知** → ground truth 可以手写，不依赖工具
- **规模受控** → 秒级跑完，能进 CI

我们不需要 10 种语言。Python 单语言起步即可，**域照抄**（auth / token / middleware / service / repo），
这样将来加语言能直接对齐。

### 1.2 场景清单（13 个，直接复用）

| # | 问题 | 关键差异点 |
|---|---|---|
| 01 | 谁调用 `validate_token`？ | `refs --kind calls` vs grep 字符串匹配 |
| 02 | `auth/service.py` 里有什么？ | `outline`（只要结构）vs `cat`（全文） |
| 03 | 重命名 `AuthService` 的影响？ | 传递闭包 BFS vs grep 扁平 |
| 04 | `BaseService` 的类层级？ | 结构化父子对 vs grep class 关键字 |
| 05 | 追踪 login → DB 调用链 | 连续 `callees` vs 多轮 grep |
| 06 | 文件依赖？ | 已解析目标 vs grep import 关键字 |
| 07 | 类型引用 X 的地方？ | 上下文感知 vs grep |
| 08 | 找所有 token 相关符号 | 排序+去重 vs grep 原始行 |
| 09 | 消歧 `validate`（4+ 定义） | 按文件区分 vs grep 一堵墙 |
| 10 | 高扇出：谁用了 `get_logger`？ | 精确调用点 vs 每次提及 |
| 11 | 深调用链（5+ 跳） | 顺序 callees ×6 vs 6 轮 grep |
| 12 | depth 5 的深影响 | 传递 BFS vs 扁平 grep |
| 13 | 找认证逻辑（自然语言） | 语义检索 vs grep 关键字 |

**第 09 和 10 条是专门设计来打脸的** —— 消歧和高扇出正是 grep 表现最差、
也是最能体现图价值的地方。**做 benchmark 时一定要包含自己可能输的场景**，
否则就是自证。

### 1.3 ground truth 格式（可直接照搬）

`benchmarks/ground_truth/webapp_py.json` 的真实结构：

```json
{
  "01_find_callers": {
    "description": "Who calls validate_token?",
    "query": "refs validate_token --kind calls",
    "expected": [
      { "source": "auth_middleware",  "file": "middleware/auth_mw.py" },
      { "source": "get_current_user", "file": "auth/service.py" },
      { "source": "refresh_token",    "file": "auth/tokens.py" }
    ]
  },
  "02_file_structure": {
    "description": "What's in auth/service.py?",
    "query": "outline auth/service.py",
    "expected": [
      { "name": "BaseService", "kind": "class" },
      { "name": "__init__",    "kind": "method" }
    ]
  }
}
```

要点：
- **key 带序号** → 输出表格时天然有序
- **`description` + `query` 同存** → 人读 description，脚本执行 query
- **`expected` 是 `{name, file}` 对，不是行号** → 行号会漂移，符号+文件稳定

### 1.4 四个指标维度

| 维度 | 指标 | 测法 |
|---|---|---|
| Token 效率 | 输出 bytes / 近似 token | 同一问题下 cartog vs grep 输出大小 |
| 准确率 | **对 ground truth 的 recall** | 各方案命中的 expected 条目数 |
| 命令数 | 需要的工具调用次数 | 各方案完成同一问题要几条命令 |
| 查询延迟 | µs/query | 在已索引 fixture 上的 criterion 基准 |

**注意它测的是 recall 而不是 accuracy。** 对检索系统这是正确的选择——
漏召回比多召回危害大得多（漏了调用点 = 改坏线上代码；多召回只是多读几行）。
这个取舍要在文档里明说。

### 1.5 索引隔离（一个真实的坑）

cartog 踩过并写进文档的坑，我们一定会遇到：

> fixture 在仓库内部，裸跑 `cartog index .` 会向上查找并写到仓库根的 `.cartog`，
> **每个 fixture 覆盖下一个**，recall 实际测的是最后索引的那个。
> 解法：`token_savings.sh` 和场景脚本把 `CARTOG_DB` 钉到
> `benchmarks/.indexes/<fixture>` 下（gitignored）。

**对应到我们：每个 fixture 必须有独立的 SQLite 文件 / 独立的 Neo4j database，绝不能共用。**
这类 bug 的表现是"测试通过但数字是错的"，比测试失败危险得多。

---

## 2. 第二层：Agent 端到端 benchmark（`agent/` 套件）

Shell 层证明"单次查询更好"，但不能证明"agent 用了之后整体更好"。所以有第二层：

> *"The agent-task suite drives a real agent through whole tasks **with and without**
> cartog — the end-to-end 'does it actually help' claim."*

设计要点：
- **A/B**：同一批任务，一组给工具，一组只给 grep+read
- 度量**端到端 token 成本**，不是单次查询
- 同时统计工具调用轮数、任务成功率

这是回答 "你的 MCP 到底省不省钱" 的唯一可信证据。
codegraph 公布的 `~280 vs ~1,700 tokens/query` 就属于这一层的结论。

---

## 3. 第三层：外部基准（ContextBench）—— 最有说服力的一层

### 3.1 它是什么

**ContextBench**（arXiv 2602.05892）：1136 个 issue-resolution 任务，覆盖 66 个 repo / 8 种语言，
每个都带**人工标注的 gold context**。它衡量的是
**"agent 有没有找对代码"**（file / symbol / span / line 的 coverage 与 precision），
**而不是 patch 有没有通过测试**。

这个区分很关键：**评测检索质量不应该让"代码写得好不好"污染信号。**
一个检索全对但补丁写错的 run，在 SWE-bench 上是 0 分，在 ContextBench 上是满分——
后者才是我们该测的。

### 3.2 适配方式（"Mode A"：我们作为 retriever）

```
对每个任务：
  checkout repo 到 base commit
  → 用我们的工具索引
  → 用 issue 文本查询
  → 产出 ContextBench 格式的 prediction JSONL
  → 跑官方 evaluator 打分
```

**零 LLM 编辑循环**，纯测检索。

### 3.3 官方指标

输出：file / symbol / span / line 四级的 **Coverage + Precision**，
外加 **trajectory AUC** 和 **EditLoc**。

### 3.4 真实结果长什么样（cartog 公开数据，2026-06-10，38 实例）

```
| Language   | n  | File cov | File ceiling | File prec | Span cov | Span prec | Line cov |
| rust       | 3  | 1.000    | 1.000        | 0.117     | 0.534     | 0.254     | 0.511    |
| go         | 3  | 0.833    | 0.917        | 0.126     | 0.314     | 0.135     | 0.256    |
| java       | 4  | 0.596    | 0.929        | 0.392     | 0.075     | 0.404     | 0.063    |
| typescript | 6  | 0.533    | 0.833        | 0.101     | 0.168     | 0.071     | 0.210    |
| python     | 22 | 0.504    | 1.000        | 0.103     | 0.277     | 0.075     | 0.256    |
| ALL        | 38 | 0.583    | 0.960        | 0.136     | 0.262     | 0.128     | 0.249    |
```

**读法（这是本文最重要的实践教训）：**

1. **coverage 高、precision 低是预期形态，不是失败。**
   cartog 明说：*"cartog is built for high recall retrieval... expect strong file/span
   coverage and lower precision — exactly the recall-over-precision tradeoff the
   ContextBench paper identifies across agents."*
   → **影响分析工具就该是这个形状**：宁可多报一个可疑调用点，不能漏掉真正的调用点。
2. **`File ceiling` 列是关键的可信度设计** —— 它标出"理论上能达到的最好分数"。
   有了它，0.504 的 python file cov 就有了参照（ceiling 1.000，说明还有空间，不是题出错了）。
3. **n 很小（python 22，rust 3）。** 诚实披露样本量，别拿 n=3 的语言下结论。

### 3.5 消融实验必须有

cartog 做了 FTS-only vs hybrid（关掉向量检索）的对照，结论是：
向量检索**没有**带来显著提升（ALL: 0.583 → 0.551 file cov）。

> **把对自己不利的消融结果也写出来。** 这一条的说服力超过所有正面数字。

### 3.6 五个价值千金的 gotcha（全部来自实战）

1. **gold 要喂 JSONL 而不是 parquet。** 官方 parquet loader 硬编码了
   `repo_url` 列，而 verified 子集没有这个字段，直接 `ArrowInvalid` 崩。
   需要自己写 `prepare_gold.py`，从 `original_inst_id`（`owner__repo-N`）
   反推 `https://github.com/owner/repo.git`。
2. **span precision 对输出粒度极度敏感。** 返回整个 module/class body 会把
   precision 打穿。适配层要丢掉 `module`/`import` kind 的 span，
   并给单个 span 设上限（默认 200 行）——**但保留 symbol 名字**，
   这样 symbol 级 recall 不受影响。
3. **空 gold symbol 会造成虚高。** 有些 python gold 标注的是文件头 / import 区，
   不与 tree-sitter 的 function/class 节点重叠 → 抽不出 gold symbol →
   symbol 级 Coverage/Precision 读成 **1.000/1.000 的空转**。
   → **这些实例只能信 file + span + EditLoc 三个轴。**
4. **别把 issue 原文直接当查询。** 这是最反直觉的一条：
   散文里的停用词（`changes`、`however`、`Related`）会优先匹配到
   **markdown 符号**（CHANGELOG、docs），然后 `CodeOnly` 过滤器把所有 Document 丢掉
   → **0 结果**。实测：原文 → 0 结果；抽 12 个代码标识符 → 6+ 结果。
   → 必须做 **`--keyword-query`**：从 issue 文本里抽反引号片段、CamelCase、snake_case。
5. **按 repo+commit 缓存索引。** 大仓（django、transformers）的 clone + index
   主导了整个墙钟时间。

> 第 4 条对我们有直接映射：我们当前的 `parse_intent` 用 `words[-1]` 猜函数名，
> 就是同一个问题的反面形态——**"把自然语言当结构化查询"必翻车**，
> 正确做法是从中提取标识符（`extract_identifiers`），而不是取最后一个词。

---

## 4. 第四层：Agent 轨迹分析（LLMOps）

这一层对应你列出的"轨迹分析"加分项，也是本项目 `agent.py` 的 LangGraph 状态机
天然该被检验的地方。

### 4.1 我们要测什么

当前 `agent.py` 的 `review` 节点号称"降低幻觉率"，但：

- **没有任何数字**。
- `check_review_outcome` 的 `or state["revision_count"] >= 1` 意味着
  **最多只重试 1 次就无条件放行** —— 第二次 FAIL 也会被判 "end"。
  → "自修正" 在工作流层面其实是个**半程修正**。

**必须先建轨迹埋点，才能谈"降低幻觉率"。**

### 4.2 轨迹 schema（最小可用）

每个 run 记录一条：

```json
{
  "run_id": "uuid",
  "query": "修改 process_data 会影响谁",
  "nodes_visited": ["parse", "retrieve", "generate", "review", "generate", "review"],
  "revision_count": 1,
  "retrieved_symbols": ["validate_input", "save_to_db", "main"],
  "claimed_symbols": ["validate_input", "save_to_db", "main"],
  "review_verdicts": ["FAIL: 遗漏上游", "PASS"],
  "tokens_in": 0, "tokens_out": 0,
  "latency_ms": 0,
  "terminated_by": "max_revision" | "pass" | "error"
}
```

### 4.3 三个可算的指标（这就是"幻觉率"的落地形态）

**a) 幻觉率（Hallucination Rate）**
```
hallucinated = claimed_symbols - retrieved_symbols
hallucination_rate = |hallucinated| / |claimed_symbols|
```
**纯字符串集合运算，不需要 LLM judge。** 这是最便宜也最硬的一个指标，
而且直击"基于图谱的确定性"这个核心卖点——**如果图里没有的东西出现在答案里，
那就是幻觉，可判定。**

**b) 覆盖率（Grounding Recall）**
```
recall = |claimed ∩ retrieved| / |retrieved|
```
答案覆盖了多少图事实（漏报检测）。

**c) 修正有效率（Revision Effectiveness）**
```
effectiveness = P(hallucination_rate 在一次 review 后下降)
```
这个指标才真正回答 **"自修正循环到底有没有用"**。
如果它接近 0，就该删掉 `review` 节点——**用数据决定架构，而不是因为 prompt 里写着 "Critic"。**

### 4.4 关键：把 `terminated_by` 记下来

当前实现里 `max_revision` 和 `pass` 在最终输出上无法区分。
**埋点后你很可能会发现大部分 run 是 `max_revision` 结束的**——
那 " Selbst-Correction 有效" 这个说法就要重写。这是好事，早发现早改。

### 4.5 轨迹质量分析（外部参照）

`agrawalneel25/agent-trajectory-quality-metrics` 做的是 SWE-bench 2500 条 agent
轨迹的**解析 + 可复现报告**。可借鉴的两点：

1. **轨迹解析要 robust** —— agent 输出格式不稳定，解析器本身要有容错和统计
   （多少条解析失败必须公开）。
2. **报告要可复现** —— 评测脚本输出的是报告，不只是一堆数字。

---

## 5. 评测金字塔总结

```
第 4 层  轨迹/幻觉率     → 证明 "自修正真的有效"          （我们独有，LangGraph 天然可埋点）
第 3 层  外部基准        → 证明 "在真实 issue 上找得对"     ContextBench，最有说服力
第 2 层  Agent 端到端    → 证明 "用了更省"                  A/B，含 token 成本
第 1 层  Shell 13 场景   → 证明 "单次查询比 grep 好"        最便宜，进 CI
```

**建议执行顺序：1 → 4 → 2 → 3。**

理由：第 1 层一天能做完且立刻可进 CI；第 4 层是我们 **唯一可能比竞品做得好的地方**
（竞品都没有 LLM 生成环节，所以它们没有"幻觉率"这个概念，也不会去测）；
第 2、3 层依赖前面稳定后再投入。

---

## 6. README 该怎么改（对应硬伤 #9）

**删掉：**
- "确定性 (Deterministic) 分析" → 改为 "基于静态解析的调用图分析（覆盖率见 benchmark）"
- "AST + Vector 混合检索" → **Vector 不存在，直接删**。或标注 `[roadmap]`
- "Code Property Graph" → 改 "Code Knowledge Graph (CKG)"，除非做了 CFG/PDG
- "跨文件引用" → 改为 "跨文件 import 解析"，且必须在真的做了 import 解析之后写
- `pyproject.toml` 的 `description = "Add your description here"` → 必修

**加上（这才是 README 该有的内容）：**

```markdown
## 准确率

在 psf/requests 上，跨文件依赖覆盖率 X%（n=..., ceiling=...），
残差 Y% 全部为动态派发 / 反射入口，明细见 benchmarks/residuals.md。
Python 单语言，其余语言尚未验证。

## 与 grep 对比（13 场景，fixture: benchmarks/fixtures/webapp_py）

| 指标        | grep + read | codegraph-pilot |
|-------------|-------------|-----------------|
| tokens/query| ~1,700      | ~XXX            |
| recall      | 78%         | XX%             |
| 命令数      | 6           | 1               |

## 幻觉率（Agent 轨迹，n=...）

未经 review 节点修正：X.X%；经 1 次修正后：X.X%；修正有效率：X%
```

> **"Python 单语言，其余语言尚未验证" 这句话必须留着。**
> 限定作用域的自谦，在技术上比无限定的吹嘘更有分量。

---

## 附：一手来源

- `jrollin/cartog` `benchmarks/README.md` — 13 场景设计、ground_truth 结构、指标矩阵、索引隔离坑
- `jrollin/cartog` `benchmarks/ground_truth/*.json` — ground truth 的 JSON 形态
- `jrollin/cartog` `benchmarks/contextbench/README.md` — 外部基准适配 + 5 个 gotcha + 真实结果表
- ContextBench, arXiv 2602.05892 — 1136 任务 / 66 repo / 人工 gold context
- `agrawalneel25/agent-trajectory-quality-metrics` — 2500 条 SWE-bench 轨迹的解析与报告
