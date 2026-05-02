# CodeGraph Pilot - Agent 指引

## 快速开始

```bash
docker-compose up -d          # 启动 Neo4j (等待 20 秒就绪)
cp .env.example .env          # 配置 QWEN_API_KEY
uv sync                        # 安装依赖
uv run streamlit run src/app.py
```

## 关键事实

- **依赖管理**: `uv` (pyproject.toml)，Python >= 3.10
- **Neo4j**: 默认连接 `bolt://localhost:7687`，认证 `neo4j/password123`
- **LLM**: Qwen3.5-27b (agent.py:26)，embedding 用 text-embedding-v3 (1024 维)
- **向量搜索**: FAISS + OpenAI 兼容 API (复用 Qwen 配置)

## 项目结构

```
src/
├── agent.py          # LangGraph 工作流 (parse→retrieve→generate→review)
├── graph_builder.py  # AST 解析 (CodeAnalyzer) + Neo4j 加载 (GraphLoader)
├── embedding.py       # 向量索引 (EmbeddingStore) + 语义搜索
└── app.py            # Streamlit 前端 (双 Tab: 问答 + 图谱可视化)
```

## 架构要点

- **Agent 状态**: `query`, `intent`, `target_func`, `target_func2`, `context`, `response`, `revision_count`, `feedback`, `entities`
- **意图分类**: `impact_analysis` / `call_chain` / `semantic_search` / `function_info`
- **Neo4j 节点**: `:Function`, `:Class`, `:Method` (方法命名为 `ClassName.method_name`)
- **关系类型**: `:CALLS`, `:INHERITS`, `:BELONGS_TO`
- **递归查询**: `query_recursive_impact()` 支持多跳依赖分析 (默认 max_depth=5)

## 开发注意事项

- `build_graph_for_batch()` 每次调用会先 `clean_db()` 清空数据库
- 如需增量导入，修改 `graph_builder.py:227` 注释掉 `loader.clean_db()`
- 中文注释和文档字符串，snake_case 函数，PascalCase 类
- 无测试框架配置，无 CI 配置

## Agent skills

### Issue tracker

Local markdown in `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

3 个标签：`todo` / `doing` / `wontfix`。See `docs/agents/triage-labels.md`.

### Domain docs

Single-context。See `docs/agents/domain.md`.

## 常用命令

```bash
uv run streamlit run src/app.py              # 运行应用
uv run python -c "from src.agent import app_graph; print('OK')"  # 验证导入
docker-compose down && docker-compose up -d  # 重启 Neo4j
```
