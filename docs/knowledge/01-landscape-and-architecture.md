# 代码知识图谱：领域全景与架构决策

> 资料来源：2026-10 对 GitHub 上活跃项目的实地调研（README / benchmark 源码一手抓取），
> 以及 Joern、ShiftLeft CPG 规范等上游项目。所有带数字的说法都标注了出处。

---

## 0. 先看市场：这个方向已经不是空位，但赛道在分化

一句话结论：**"代码知识图谱 + 给 Agent 用" 已经被验证是刚需（头部项目 73k star），
但竞争焦点已经从 "能不能建图" 转移到 "建的图准不准、查一次要多少 token、能不能增量更新"。**

### 竞品分层

| 项目 | 语言/实现 | Star | 定位关键词 |
|---|---|---|---|
| `colbymchenry/codegraph` | Rust 内核 + TS 外壳 | ~73k | 预索引、自动同步、**单 MCP 工具**、按语言公布覆盖率 |
| `jrollin/cartog` | Rust + SQLite | 18 | **量化最狠**：13 场景 benchmark + ContextBench 外部评测 + LSP 精化 |
| `sshaaf/rgctl` | Rust | 22 | 30+ 关系类型、CFG/数据流/污点、`blast-radius`、CI 门禁 |
| `cajasmota/grafel` | Go daemon | 23 | 多仓跨仓、infra→code 血缘、22 个 MCP 工具 |
| `justrach/devswarm` | Zig | 67 | 高性能 MCP + 图引擎 |

**读出来的三条共识（这是本文最有价值的部分）：**

1. **主流都用 SQLite / 单文件二进制图快照**（`.cartog`、`.codegraph/codegraph.db`、`.grafel/graph.fb`），
   零个用图数据库。理由很实在：开发者机器上跑一个 Docker + Neo4j 是巨大的使用摩擦；
   "一键 `curl | sh` 然后两条命令" 才是能扩散的形态。
   → **但这条有适用边界**：它们都是被别人调用的 **MCP 工具**，本项目是自带编排的 **Agent 应用**。
   详见 §3 的修正。**结论：Neo4j 保留。**
2. **交付形态统一是 CLI 优先 + MCP 第二 + 可选 Dashboard。** Streamlit 单独作为入口在这个赛道里几乎没有先例——
   它是"演示形态"，不是"使用形态"。
   → **这条对本项目的含义不是"砍掉 UI"，而是"UI 不该是唯一入口"**：
   应当补一个 CLI/MCP 面，让编排能力能被外部 agent 调用。UI 是我们的加分项，不是错误。
3. **tree-sitter 是事实标准**，不是 Python `ast`。多语言靠 tree-sitter grammar；`ast` 只能做单语言原型。

### 关键洞察：单工具 vs 工具箱，行业答案出人意料

codegraph（73k star）的做法值得单独拿出来：

> 它把 7 个已实现工具（`node`/`search`/`callers`/`callees`/`impact`/`files`/`status`）
> **默认全部不暴露**，只暴露一个 `codegraph_explore`。理由是：
> *"Measured agent behavior showed that one strong tool steers agents better than a menu of
> narrower ones — fewer mis-picks, and it saves context every session."*

而 grafel 走了反面：**22 个 MCP 工具**。

两条路都有人做，但 codegraph 给出了**测量依据**（agent 选错工具的比率 + 每次会话的 context 开销）。
这给我们一个可直接复用的工程原则：

> **工具数量是一个需要被 benchmark 的决策，不是审美决策。**
> 面向 Agent 的接口，默认值应该收敛；拆分只在有证据显示 agent 会 mis-pick 时才做。

---

## 1. 概念正名：你用的不是 CPG

README 里写 "Code Property Graph" 是被挑刺的高危点，因为 CPG 有**精确定义**（Yamaguchi et al., 2014）：

**CPG = AST ∪ CFG ∪ PDG（+ 可选 call graph）的超图，节点带属性（properties）。**

| 层 | 全称 | 回答的问题 | 我们的 `graph_builder.py` 有吗 |
|---|---|---|---|
| AST | Abstract Syntax Tree | 语法结构：谁定义了什么 | ✅ 部分（Class/Function/Method） |
| CFG | Control Flow Graph | 执行顺序、分支 | ❌ |
| PDG | Program Dependence Graph | 数据依赖 + 控制依赖 | ❌ |
| CG | Call Graph | 调用关系 | ⚠️ 有边但**靠名字匹配**，不是真解析 |
| Properties | 节点属性 | 代码度量、签名、类型 | ⚠️ 只有 filepath/lineno |

**所以准确的说法是：我们建的是 Call Graph + 少量继承边，连 "Property Graph" 的 property 层都没有。**

推荐的正名路径（二选一，别再叫 CPG）：
- 保守：**Code Knowledge Graph (CKG)** —— 这是赛道里通用叫法，无争议。
- 进取：**保留 CPG 名字，但把 CFG/PDG 做出来**。`rgctl` 就是这么干的（`slice`/`inspect`/`cpg`/`taint` 需要 `discover --with-cfg`）。

### 属性图 vs RDF/OWL：什么时候该上语义网

这是你列出的加分项，说清楚边界比堆概念重要：

| | 属性图（Neo4j / SQLite 图） | RDF + OWL（GraphDB / Jena / RDFLib） |
|---|---|---|
| 数据模型 | 节点/边都可挂任意 kv 属性 | 三元组 (s,p,o)，边就是节点 |
| 查询 | Cypher / 自定义遍历 | SPARQL |
| 推理 | **无内置推理** | **有**：RDFS/OWL 推理机可做子类传递、等价、约束校验 |
| 性能 | 遍历快，适合百万边 | 推理有代价，但语义约束强 |
| 业界用于代码 | **全部主流项目**（cartog/codegraph/grafel/rgctl） | 几乎没有 |

**判断规则（可以直接写进设计文档）：**

- 你的核心查询是**图遍历**（谁调用我、影响半径、最短路径）→ 用属性图，**不要上 OWL**。
- 你需要的是**跨项目/跨语言的概念对齐 + 约束校验 + 自动分类**（例如"这个符号同时是 HTTP handler 又是 DB 写入点，
  按策略必须打标"）→ 这才是 Ontology 的用武之地。

**一个真实可落地的折中（这是加分项的正确打开方式）：**
用 **OWL/RDFS 定义 schema 层（TBox）**，把 "Function / Method / Class / Module / Endpoint"
以及约束（如 `CALLS` 的 domain/range、`INHERITS` 必须是 Class→Class）形式化，
用它做**图数据的合规校验**，运行时查询仍然走属性图/Cypher。
这样你能同时说："我有本体建模（schema + 语义约束），并且查询层做了正确的技术选型。"

这也是我们当前代码的一个真实缺陷修复点：
`MATCH (src {name: $src})` 不限定 label，导致 `:Function` 和 `:Class` 同名互相串——
**这在本体里就是一个 domain/range 约束被违反**，可以用 SHACL/OWL 约束静态检查出来。

---

## 2. 解析层：从 `ast.NodeVisitor` 到真实可用的图

### 2.1 现在的解析器缺什么

对照 `graph_builder.py`，要补齐的硬事实：

| 缺失 | 后果 | 修法 |
|---|---|---|
| **没有 File/Module 节点** | 无法表达"文件级依赖"，无法做增量 | 建 `:Module`，`(:Symbol)-[:DEFINED_IN]->(:Module)` |
| **没有 import 解析** | 跨文件 CALLS 靠"名字相同"猜，README 的"跨文件引用"是假的 | 解析 `Import`/`ImportFrom`，建 `:IMPORTS` 边，再按 module 路径解析符号 |
| **`obj.method()` 只记方法名** | `Repo.save()` 和 `Cache.save()` 全串到同一个 `save` | 用**类型推断**（局部变量赋值来源）降级处理：推断不出就记为 `UNRESOLVED:save`，**别假装解析成功** |
| **只查 CALLS** | INHERITS / BELONGS_TO 白建 | 查询层要吃继承（含多态：改基类方法 → 影响所有子类） |
| **无增量** | 每次全库 `DETACH DELETE`，repo 级不可用 | 按文件 hash/mtime 做 dirty 检测，只删该文件的子图后重建 |

### 2.2 解析质量的量化方式（照抄 cartog 的定义）

cartog 给的指标定义可以直接搬：

> **Fair coverage** = 拥有至少一个 *已解析的跨文件依赖方*（import / call / reference / 框架路由）
> 的"含符号源文件"占比，在真实 repo 上按语言分别测量。
> 残差必须诚实披露（运行时动态派发、反射/DI 容器、框架约定入口、vendored 第三方代码），
> **绝不能靠玩分母来掩盖**。

它公布的实测（每语言一个真实开源 repo）：

```
Python  psf/requests      100%     Go     gin-gonic/gin  96.6%
Rust    BurntSushi/ripgrep 86.7%    Java   google/gson   93.3%
C       redis/redis       92.2%     PHP    guzzle/guzzle 100%
```

框架路由单独测：Express 100%、FastAPI 98%、Flask 100%、Django 74.1%、Spring 83.3%。
**注意它敢把 Django 的 74.1% 和 ASP.NET 的 83.9% 写出来**，并说明原因是 "reflection/DI 的静态分析天花板"。

> 这就是第 10 条硬伤（零量化）的标准答案：
> **不是"我们的准确率 99%"，而是"我们在 psf/requests 上跨文件覆盖率 X%，残差 Y% 全是动态派发，已逐条列出"。**
> 后者在面试官眼里比前者高一个量级。

### 2.3 解析精度杠杆：LSP 是免费的数量级提升

cartog 最有价值的一条工程经验：

> 自动探测 PATH 上的 language server（pyright / rust-analyzer / gopls / ...），
> 用它把边的**解析率从 ~25% 提升到最高 81%**。结果持久化到 SQLite，**成本只付一次**。

`--no-lsp` 的启发式解析率只有 **25–37%**，LSP 加持后 **97% recall**。

对我们的直接含义：**Python 场景下接 `pyright`（或 jedi）做符号解析，是投入产出比最高的一件事。**
`ast` 只能看到语法，LSP 能看到"这个 `save` 到底是哪个类的 `save`"。

---

## 3. 存储层：Neo4j 该留还是该换

这是本项目最大的架构抉择，不回避。

> **修正 (2026-10-14)：** 早期版本把竞品的 SQLite 选型直接外推为本项目结论，
> 这是**过度外推**。竞品全是「MCP 工具」，本项目是「带 UI 的 Agent 应用」，
> 二者的部署约束不同。本节已按「Neo4j 保留为默认后端」重写，
> `GraphStore` 抽象仍然保留，但目的改为「为将来做 MCP 分发预留第二后端」。

### 3.1 行业数据（及其适用边界）

- 4 个主流项目**全部用 SQLite / 单文件二进制**，零个用图数据库。
- cartog 明说：**"One static binary, one SQLite file. No Python, no pip, no Docker, no cloud: 100% local by default."**
- 查询延迟对比：cartog 结构查询 **8 µs – 20 ms**（SQLite + Rust）；Neo4j 单次 bolt 查询通常在 ms–数十 ms 级，
  还要加上一次网络往返 + 连接池开销。

**但这条数据不能直接套用到本项目，原因是产品形态不同：**

| | 竞品（codegraph / cartog / rgctl / grafel） | 本项目 |
|---|---|---|
| 形态 | **MCP 工具** —— 被别人的 agent 调用 | **Agent 应用** —— 自带编排与 UI |
| 分发约束 | 必须 `curl | sh` 一键装到开发者机器 → 零依赖是硬需求 | 用户已接受 `docker-compose up` |
| 查询模式 | 单次符号查询（`refs` / `impact`） | **变长路径 + 双向遍历 + 多跳推理** |
| 是否需要 Cypher | 不需要，自己写 BFS | **需要，Cypher 是核心能力** |

**结论：竞品的零依赖约束来自「要分发到别人的机器上」，本项目没有这个约束。**
把「工具的部署约束」当成「本项目的架构约束」是过度外推。

### 3.2 诚实的取舍

| | Neo4j | SQLite / 单文件 |
|---|---|---|
| 上手摩擦 | 需 Docker，7687 + 7474 | 零 |
| 可视化 | **Neo4j Browser 白送**，生态成熟 | 要自己做 |
| Cypher | **声明式图查询，表达力强**（变长路径、模式匹配） | 要手写递归 CTE / BFS |
| 部署到 Agent 平台 | 重 | 轻 |
| 多仓/大仓 | 合适 | 单文件也够（图规模其实不大） |

**Cypher 的变长路径就是"确定性分析"这一卖点本身：**

```cypher
MATCH (a:Function {name: $name})-[:CALLS*1..5]->(b) RETURN DISTINCT b
```

一行完成 5 跳传递闭包；同样的事在 SQLite 里要写递归 CTE 或手写 BFS。
**换掉 Neo4j 会自断一臂——它不只是存储，它是检索能力的来源。**

### 3.3 结论：保留 Neo4j，抽象层为将来预留

1. **Neo4j 继续作为默认后端**，Cypher 作为一等查询能力保留并强化。
2. **引入 `GraphStore` 协议**，把查询收进抽象层（见 §5 优先级 #1 的修订版）——
   目的**不是**替换 Neo4j，而是：
   - 让解析层与存储层解耦，便于**离线测试**（不连数据库也能测图算法）；
   - 为**将来做 MCP 分发**时预留一个轻量 SQLite 后端的位置，届时按需实现。
3. 前端与 agent 都通过 `GraphStore` 访问，不再各自直连驱动
   —— 顺带修掉硬伤 #8（每次查询新建 `GraphDatabase.driver`）。

> **判断标准**：只有当"要分发给别人当 MCP 工具用"成为明确目标时，才实现 SQLite 后端。
> 在那之前，`GraphStore` 的价值是**可测性 + 解耦**，不是换库。

---

## 4. 交付形态：MCP / CLI / 插件

### 4.1 形态选择的行业答案

主流是 **CLI + MCP**，且都提供 `install` 子命令自动写配置：

```bash
cartog ide                       # 自动给所有检测到的 editor 写 MCP 配置
cartog install cursor claude codex
rgctl install --skill --tools cursor,claude,codex,agents
```

配置形态（以 Codex CLI 为例，`~/.codex/config.toml`）：

```toml
[mcp_servers.cartog]
command = "cartog"
args = ["serve", "--watch"]
```

### 4.2 一个我们独有的机会：Claude Code 只给 `--watch`

cartog 的细节很值得学：

> *"Only Claude Code gets `--watch` by default — the others ship plain `["serve"]`.
> Agent-driven flows churn files faster than human-driven editor flows, so the
> in-process file watcher pays off."*

**这是"按使用场景差异化配置"的范例**，也说明这些项目是拿真实 agent 行为数据在做决策。

### 4.3 MCP 输出设计（直接影响是否被 agent 采纳）

三条可直接抄的规则：

1. **Compact by default**：返回 `locations + signatures + snippet previews`，**不是全文**。
   cartog 的 `--compact` 让 payload 小 ~60% 但仍带符号/签名/分数。
   `CARTOG_MCP_COMPACT=0` 才恢复全文。
2. **永远提供 JSON 输出**：`rgctl` 明确写 *"Always prefer `-f json` for agents and scripts.
   Do not scrape stderr."*
3. **Token budget 是一等公民**：`cartog map --tokens 4000`、grafel 返回 "token-budgeted subgraph"。
   **让调用方指定预算，而不是你替它决定。**

codegraph 公布的对比数字可以直接作为我们的目标值：

```
grep + read      ~1,700 tokens/query    recall 78%
cartog           ~280  tokens/query     recall 97%
```

**"~280 tokens vs ~1,700，recall 78% → 97%" 就是这类项目的 headline metric。**
我们做出来之后应该有自己的这一行。

### 4.4 别漏掉"教 agent 怎么用"

cartog 有一节叫 *"Teach your agent to use cartog"*，提供 `docs/agent-snippet.md`
让用户粘进项目 `AGENTS.md`；grafel 提供 `CLAUDE.md`（When to use MCP vs grep）。

> **洞察：工具的 adoption 瓶颈不是能不能调，是 agent 知不知道什么时候该调它。**
> 提供一份 "prefer graph over grep when..." 的 snippet，是低成本高回报的差异化。

这正好可以复用我们仓库已有的 `AGENTS.md`——我们自己的项目就是第一个试验田。

---

## 5. 给本项目的优先级建议（按 ROI 排序）

> 已按「保留 Neo4j」修订（2026-10-14）。原 #1「SQLite 后端」降级为条件触发项。

| # | 动作 | 解决哪条硬伤 | 为什么这个值 |
|---|---|---|---|
| 1 | **导入 `File/Module` 节点 + 解析 import + 增量更新** | 3、4、5 | 当前图是假的（跨文件靠名字猜）；**这一步做完图才"真"**，是所有上层能力的地基 |
| 2 | **建 benchmark：13 场景 + ground_truth JSON + 覆盖率表** | 10（零量化） | 直接抄 cartog 结构，是本轮最能在面试中拉开差距的 |
| 3 | **`GraphStore` 协议（Neo4j 为默认实现）** | 8（连接管理）、可测性 | 复用 driver、让图算法能离线测；**不换库**，只为解耦 + 为将来 MCP 分发留位 |
| 4 | **意图识别从 `words[-1]` 换成结构化标识符抽取** | 1 | 小改动，但是"玩具感"最明显的单点 |
| 5 | **接 pyright 做符号解析** | 4 | 解析率 25% → 80%+，有现成数据支撑 |
| 6 | **Agent 轨迹埋点 + 幻觉率/修正有效率** | 10、9 | **竞品都没有 LLM 生成环节，这是我们的独有指标**（见 02 文档第 4 层） |
| 7 | **补 CLI / MCP 面（UI 保留）** | 交付形态 | 让编排能力能被外部 agent 调用，UI 作为加分项保留 |
| 8 | 补 CFG/PDG 后改叫 CPG | 9 | 最贵，放最后；先用 CKG 正名过渡 |
| — | *(条件)* SQLite 后端 | 可用性 | **仅当明确要做 MCP 单文件分发时才实现** |

**建议先做 1 + 2。** 理由：

- #1 是当前唯一"结构性错误" —— 其余硬伤多是打磨，跨文件调用靠猜是**结论不可信**级别的问题。
- #2 是差异化 —— **当前赛道里 4 个竞品只有 1 个（cartog）真的做了量化评测，
  而且它靠这个拿到了差异化叙事。** 第 10 条硬伤最容易被补、也最容易被低估。

### 什么不该动

- **Neo4j 保留**：Cypher 变长路径是"确定性分析"这一卖点的来源（§3）。
- **LangGraph 编排保留**：竞品全是无 LLM 的检索引擎，没有编排层。
  `parse → retrieve → generate → review` 是我们的差异化，不是技术债。
- **Streamlit UI 保留**：它不是错误，只是不该是唯一入口（补 CLI/MCP 即可）。

---

## 附：一手来源清单

- `colbymchenry/codegraph` — MCP 工具收敛策略、按语言覆盖率表、SQLite 存储
- `jrollin/cartog` — `benchmarks/README.md`、`ground_truth/*.json`、`contextbench/README.md`、LSP 解析率数据
- `sshaaf/rgctl` — 30+ 关系类型、CFG/污点/CI 门禁命令矩阵
- `cajasmota/grafel` — daemon 架构、跨仓血缘、22 MCP 工具、agent-snippet 思路
- `joernio/joern`、`ShiftLeftSecurity/codepropertygraph` — CPG 的权威定义
- `EuniAI/ContextBench`（arXiv 2602.05892）— 1136 个 issue 任务、人工标注 gold context 的外部评测集
