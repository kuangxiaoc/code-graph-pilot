# CodeGraph Pilot - 项目上下文

## 项目概述

CodeGraph Pilot 是一个**面向复杂工程的代码理解与重构辅助系统**。通过将 Python 源代码转化为 Code Property Graph (代码属性图) 并存入 Neo4j，结合 LLM 的推理能力，实现对代码修改影响范围的**确定性分析**。

### 核心价值
- 解决传统 RAG 在代码领域的"语义漂移"与"结构缺失"问题
- 提供双向影响分析：下游依赖 (Outbound) + 上游影响 (Inbound)
- 基于 LangGraph 的自修正 Agent 工作流，降低幻觉率

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | Streamlit + streamlit-agraph (图谱可视化) |
| Agent 框架 | LangGraph + LangChain |
| LLM | 通义千问 Qwen-Plus (兼容 OpenAI API) |
| 图数据库 | Neo4j 5.x |
| 依赖管理 | uv (pyproject.toml) |
| 容器化 | Docker Compose |

---

## 项目结构

```
code-graph-pilot/
├── src/
│   ├── graph_builder.py   # AST 解析器 + Neo4j 数据加载器
│   ├── agent.py           # LangGraph 状态机工作流
│   └── app.py             # Streamlit 前端应用
├── docker-compose.yml     # Neo4j 容器配置
├── pyproject.toml         # 依赖声明
├── .env                   # 环境变量 (需自行配置)
└── neo4j_data/            # Neo4j 数据持久化目录
```

---

## 核心模块详解

### 1. `src/graph_builder.py` - 静态分析引擎

**CodeAnalyzer (AST 访问者)**
- 解析 Python 源码，提取实体: `Class`, `Function`, `Method`
- 提取关系: `CALLS`, `INHERITS`, `BELONGS_TO`
- 支持类内调用识别 (`self.method()`)

**GraphLoader (Neo4j 加载器)**
- 批量写入节点和关系到 Neo4j
- 支持增量导入 (MERGE 语义)

**关键函数**
```python
build_graph_from_file(filepath, uri, user, pwd)  # 单文件导入
build_graph_for_batch(file_paths, uri, user, pwd)  # 批量导入
```

### 2. `src/agent.py` - Agent 工作流

基于 LangGraph 的状态机，包含 4 个节点:

| 节点 | 功能 |
|------|------|
| `parse` | 意图解析，提取目标函数名 |
| `retrieve` | 图谱检索，执行 Cypher 双向查询 |
| `generate` | LLM 生成分析报告 |
| `review` | 结果审查，检测幻觉 |

**工作流路由**
```
parse → retrieve → generate → review
                          ↓
                   PASS → END
                   FAIL → retry (最多 1 次)
```

**图谱查询模板** (`query_dependencies`)
```cypher
MATCH (target:Function {name: $name})
OPTIONAL MATCH (target)-[:CALLS]->(downstream)
OPTIONAL MATCH (upstream)-[:CALLS]->(target)
RETURN target, downstream, upstream
```

### 3. `src/app.py` - 前端应用

**侧边栏**
- 多文件批量上传 (`accept_multiple_files=True`)
- 一键构建知识图谱

**主界面 (双 Tab)**
- Tab 1: 对话式影响分析 (调用 Agent 工作流)
- Tab 2: 交互式图谱可视化 (streamlit-agraph)

---

## 构建与运行

### 1. 启动 Neo4j
```bash
docker-compose up -d
# 访问 http://localhost:7474 确认就绪
# 默认账号: neo4j / password123
```

### 2. 安装依赖
```bash
uv sync
```

### 3. 配置环境变量
创建 `.env` 文件:
```ini
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password123
QWEN_API_KEY=sk-xxxxxxxxxxxxxx
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

### 4. 运行应用
```bash
uv run streamlit run src/app.py
```

---

## 开发约定

### 代码风格
- 使用中文注释和文档字符串
- 函数命名采用 snake_case
- 类命名采用 PascalCase

### Neo4j 数据模型

**节点类型**
- `:Function` - 顶层函数
- `:Class` - 类定义
- `:Method` - 类方法 (命名为 `ClassName.method_name`)

**关系类型**
- `:CALLS` - 函数调用
- `:INHERITS` - 类继承
- `:BELONGS_TO` - 方法属于类

### Agent 状态定义
```python
class AgentState(TypedDict):
    query: str           # 用户原始问题
    target_func: str     # 解析出的目标函数
    context: str         # 图谱检索上下文
    response: str        # LLM 生成的回答
    revision_count: int  # 重试次数
    feedback: str        # 审查反馈
```

---

## 常见任务

### 添加新的 AST 解析规则
编辑 `graph_builder.py` 中的 `CodeAnalyzer` 类，添加新的 `visit_*` 方法。

### 扩展 Agent 能力
1. 在 `agent.py` 中定义新节点函数
2. 使用 `workflow.add_node()` 注册
3. 使用 `workflow.add_edge()` 或 `add_conditional_edges()` 连接

### 修改图谱查询逻辑
编辑 `agent.py` 中的 `query_dependencies()` 函数，调整 Cypher 查询模板。

---

## 注意事项

1. **数据清空**: `build_graph_for_batch()` 每次调用会清空数据库，如需增量导入请注释 `loader.clean_db()`
2. **API 限流**: Qwen API 有调用频率限制，批量操作时需注意
3. **图谱可视化**: 节点过多时建议调小 `limit` 参数以保持性能

---

---

## Superpowers 工作流程

技能文件位于 `.iflow/skills/` 目录，每个技能通过 `SKILL.md` 定义。以下是在 AGENTS.md 中内联的核心工作流程，必须在相应场景下遵循。

---

### 🔵 BRAINSTORMING - 设计讨论流程

**触发条件:** 任何创建性工作之前 - 创建功能、构建组件、添加新行为。

**铁律:** 在展示设计并获得用户批准之前，**禁止**编写任何代码、创建项目或执行任何实现操作。

**流程清单:**
1. **探索项目上下文** — 检查文件、文档、最近提交
2. **逐个提问澄清问题** — 理解目的、约束、成功标准
3. **提出 2-3 个方案** — 包含权衡分析和推荐理由
4. **分段展示设计** — 每段设计后确认是否正确
5. **编写设计文档** — 保存到 `docs/specs/YYYY-MM-DD-<topic>-design.md`
6. **用户审核文档** — 在开始实现计划前请用户确认

**反模式:** "这个太简单了不需要设计" — 简单项目恰恰是未审视假设导致浪费最多的地方。

**关键原则:**
- 一次只问一个问题
- 优先使用选择题而非开放式问题
- YAGNI 原则：从设计中移除不必要的功能
- 分段验证：展示设计，获得批准后再继续

---

### 🟢 TDD - 测试驱动开发

**触发条件:** 实现任何功能或修复 bug 时，在编写实现代码之前。

**铁律:**
```
没有先失败的测试，就不写生产代码
```

**RED-GREEN-REFACTOR 循环:**

```
RED (写失败测试)
    ↓
验证测试正确失败 ← 必须！
    ↓
GREEN (写最小实现)
    ↓
验证测试通过 ← 必须！
    ↓
REFACTOR (清理代码)
    ↓
重复下一个测试
```

**每个步骤:**

| 阶段 | 动作 | 验证 |
|------|------|------|
| RED | 写一个最小测试展示预期行为 | 运行测试，确认失败原因正确 |
| GREEN | 写最简单的代码让测试通过 | 运行测试，确认通过且无其他测试失败 |
| REFACTOR | 清理重复、改进命名、提取辅助函数 | 保持测试绿色，不添加新行为 |

**常见合理化借口 vs 现实:**

| 借口 | 现实 |
|------|------|
| "太简单不需要测试" | 简单代码也会出错，测试只需 30 秒 |
| "我之后再补测试" | 后补的测试立即通过，证明不了任何事 |
| "已经手动测试过了" | 手动测试 = 临时性，无记录，不可重复运行 |
| "删除 X 小时的工作太浪费" | 沉没成本谬误，保留未验证的代码才是技术债务 |

**红色警报 — 停下并重新开始:**
- 先写代码后写测试
- 测试立即通过
- "仅此一次跳过 TDD"
- "保留作为参考"

---

### 🟡 WRITING-PLANS - 编写实现计划

**触发条件:** 有规格或需求的多步骤任务，在触碰代码之前。

**流程:**
1. **范围检查** — 如果规格覆盖多个独立子系统，建议拆分为多个计划
2. **文件结构映射** — 确定要创建/修改哪些文件，每个文件负责什么
3. **任务分解** — 每个步骤是一个动作（2-5 分钟）

**任务粒度示例:**
```
- "写失败测试" — 一个步骤
- "运行确认失败" — 一个步骤
- "写最小实现让测试通过" — 一个步骤
- "运行测试确认通过" — 一个步骤
- "提交" — 一个步骤
```

**计划文档头部模板:**
```markdown
# [功能名称] 实现计划

**目标:** [一句话描述构建什么]

**架构:** [2-3 句关于方法的描述]

**技术栈:** [关键技术/库]

---
```

**任务结构模板:**
```markdown
### 任务 N: [组件名称]

**文件:**
- 创建: `path/to/file.py`
- 修改: `path/to/existing.py:123-145`
- 测试: `tests/path/test.py`

- [ ] **步骤 1: 写失败测试**
- [ ] **步骤 2: 运行测试确认失败**
- [ ] **步骤 3: 写最小实现**
- [ ] **步骤 4: 运行测试确认通过**
- [ ] **步骤 5: 提交**
```

**关键原则:**
- 始终提供精确文件路径
- 计划中包含完整代码（不要写"添加验证"这种模糊描述）
- 精确命令和预期输出
- DRY、YAGNI、TDD、频繁提交

---

### 🔴 SYSTEMATIC-DEBUGGING - 系统化调试

**触发条件:** 遇到任何 bug、测试失败或意外行为时，在提出修复之前。

**铁律:**
```
没有根因调查，就不提出修复
```

**四阶段流程:**

**阶段 1: 根因调查** (必须完成才能进入下一阶段)
1. **仔细阅读错误信息** — 通常包含精确解决方案
2. **稳定复现** — 能否可靠触发？确切步骤是什么？
3. **检查最近变更** — git diff、最近提交、新依赖、配置变更
4. **多组件系统收集证据** — 在每个组件边界添加诊断日志
5. **追踪数据流** — 坏值从哪里来？一直追溯到源头

**阶段 2: 模式分析**
1. 找到类似的工作代码
2. 完整阅读参考实现（不要略读）
3. 列出工作代码和损坏代码的所有差异
4. 理解依赖关系

**阶段 3: 假设与测试**
1. **陈述单一假设** — "我认为 X 是根因，因为 Y"
2. **最小化测试** — 一次只改一个变量
3. **验证后再继续** — 有效 → 阶段 4；无效 → 新假设
4. **不懂就说不懂** — 不要假装知道

**阶段 4: 实现**
1. **创建失败测试用例** — 最简单的复现（必须）
2. **实现单一修复** — 解决已识别的根因，一次一个改动
3. **验证修复** — 测试通过？其他测试未破坏？
4. **修复失败？** — 停下，重新分析
5. **3+ 次修复失败？** — 质疑架构，与用户讨论

**红色警报 — 停下并回到阶段 1:**
- "先快速修一下，之后再调查"
- "试试改 X 看看有没有用"
- "加多个改动一起测试"
- "跳过测试，我手动验证"
- "再试一次修复"（已经试过 2+ 次）

---

### 工作流程触发总结

| 场景 | 触发技能 |
|------|----------|
| 用户要求新功能 | brainstorming → writing-plans → executing |
| 用户要求修复 bug | systematic-debugging → TDD |
| 开始写代码 | TDD (RED-GREEN-REFACTOR) |
| 测试失败 | systematic-debugging |
| 代码写完 | verification-before-completion |

**优先级:** 用户显式指令 > Superpowers 技能 > 默认系统行为
