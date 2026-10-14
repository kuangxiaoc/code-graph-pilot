# Spec: delivery-surface (CLI / MCP 入口)

**Module id:** `delivery-surface` (见 `capability-map.md`)

## Objective

让编排能力可以不靠 Streamlit 使用。当前唯一入口是 `streamlit run src/app.py`,
它是"演示形态"; 竞品行业共识是 **CLI 优先 + MCP 第二 + 可选 Dashboard**
(docs/knowledge/01-landscape-and-architecture.md §4.1)。

本 spec 只做 **CLI**, MCP 单列条件触发 (见文末)。

## 先定义问题, 再写代码

```
FEATURE: 用户在终端里使用本项目

ON-CALL WILL ASK:
1. 只想查"改这个函数影响谁", 为什么要起一个 web 服务?   -> 无 CLI
2. 脚本里怎么调用? streamlit 无法非交互使用            -> 无编程入口
3. agent 平台怎么接?                                   -> 无 MCP
```

## 命令面 (少而收敛, 不堆命令)

```
codegraph build <path>      # 解析目录 -> 写入 Neo4j
codegraph symbol <name>     # 定位符号, 输出直接上下游 (JSON)
codegraph impact <name>     # 传递闭包影响分析 (JSON)
codegraph analyze <query>   # 跑完整 Agent 工作流 (需 LLM Key)
codegraph report            # 轨迹指标汇总
codegraph validate          # 本体约束校验
codegraph mcp               # 启动 MCP 服务 (stdio)
```

**为什么不做得更多**: codegraph 竞品 (73k star) 把 7 个工具默认全部不暴露,
只留一个 `codegraph_explore`, 理由原文是"一个强工具比一堆窄工具更能引导 agent"。
本项目的消费方是**人 + 脚本**, 6 条命令已覆盖全部已实现能力;
新增命令必须对应一个已存在的 store 方法, 不为假想需求预留。

## 输出契约

- **永远提供 `--json`**: 脚本不得解析人类可读文本 (竞品 rgctl 的明确建议)。
- 人类可读输出走 stderr 还是 stdout? **结构化数据走 stdout, 进度/告警走 stderr**,
  这样 `codegraph symbol x | jq` 干净可用。
- 失败时退出码非零, 且 stderr 有可读原因; 不得把 traceback 甩给用户。

## Boundaries

- **Always**: 新命令加在 `src/cli.py`, 不在 app.py 里长分支。
- **Never**: 在 CLI 里直连驱动 —— 一律走 `graph_store.get_store()`。
- **Ask first**: 新增命令 (扩大承诺面); 暴露 MCP (需要单独决策, 见下)。

## MCP: 已交付 (2026-10-14)

原定"条件触发", 两个条件均已满足, 故已实现:
1. 用真实 LLM Key 跑通 **30 次** analyze, 幻觉率有基线数字
   (3.6%, grounding recall 100%, 逐条审计后真幻觉 0);
2. 有明确的外部 agent 平台接入需求。

实现要点:
- 只暴露 `codegraph_query` 一个工具, `action` 分派
  (symbol / impact / hierarchy / overview)
- **错误一律返回结构化 JSON, 不抛异常**: `{"error": "⚠️ 未找到符号"}`
  是预期内的查询结果, agent 应据此换名字重试。抛异常会让它放弃整个工具
- `depth` 夹在 1..10, 防 Cypher 遍历失控
- 只暴露图查询, **不暴露 analyze**(要 LLM): MCP 的价值是给 agent 一个
  **确定性**的事实来源, 让它自己推理, 而不是套一层 LLM 生成自然语言

`GraphStore` 协议使 MCP 层只是适配器 —— 测试注入假 store 即可,
不连 Neo4j。

## Success Criteria

1. `uv run python -m codegraph <cmd>` 七条命令全部可用。
2. `--json` 输出可被 `jq` 直接消费, 人类可读信息不混入 stdout。
3. 无 `.env` 时 build/symbol/impact/validate 可用 (LLM 相关命令明确报错)。
4. 测试覆盖: 每条命令至少一个用例, 不连 LLM。
