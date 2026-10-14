# Repository Guidelines

## Project Structure & Module Organization

```
src/
  graph_builder.py   # ast.NodeVisitor 解析器 + GraphLoader (Neo4j 写入)
  agent.py           # LangGraph 状态机: parse -> retrieve -> generate -> review
  app.py             # Streamlit UI (侧边栏入库 / 对话分析 / 图谱可视化)
test_code.py         # 演示样本: 4 个互相调用的函数, 供手动入库试用
assets/              # README 截图
docs/knowledge/      # 领域调研与评测方法笔记
docker-compose.yml   # Neo4j 容器 (7474 HTTP / 7687 bolt)
pyproject.toml       # uv 依赖声明
```

Roadmap 方向 (见 `docs/knowledge/`): 存储层抽成 `GraphStore` 协议 (SQLite 默认 + Neo4j 可选),
并补齐 `Module` 节点、import 解析和增量更新。术语上用 **Code Knowledge Graph (CKG)**,
不要写 CPG——CFG/PDG 尚未实现。

## Build, Test, and Development Commands

```bash
docker-compose up -d          # 启动 Neo4j (neo4j / password123)
uv sync                        # 安装依赖 (Python 3.10, 见 .python-version)
uv run streamlit run src/app.py   # 启动前端
```

`.env` 需要提供 `NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD`、`QWEN_API_KEY`、`QWEN_BASE_URL`。

## Coding Style & Naming Conventions

- 4 空格缩进; 函数/变量 `snake_case`, 类 `PascalCase`。
- 注释与文档字符串用**中文**; 面向 LLM 的 prompt 与 Cypher 结果 key 保留英文。
- 无 ruff / black / mypy 配置。新增代码请保持与现有文件一致, 不要顺手全局重格式化。

## Testing Guidelines

**当前没有测试框架**, `pyproject.toml` 未声明 pytest, `test_code.py` 是演示样本而非测试。
新增测试请先 `uv add --dev pytest`, 放在仓库根 `tests/` 下, 命名 `test_<module>.py`,
函数以 `test_` 开头。解析逻辑要绕过 Neo4j: 直接断言 `CodeAnalyzer(path).entities / .relations`,
不连数据库。

## Commit & Pull Request Guidelines

Git 历史采用 Conventional Commits 前缀, 中文描述, 带作用域:

```
feat(core): enhance code graph parsing, batch import and interactive UI
fix(agent): 更新 Qwen 模型版本为 qwen3.5-27b
docs(readme): 修正图片路径并添加新图片资源
```

PR 需说明改动动机与影响面; 涉及 UI 或图谱输出变化时附截图 (存 `assets/`)。
涉及准确率/覆盖率的新增声明, 必须链接 `docs/knowledge/02-evaluation-and-trajectory.md`
中的评测方法并给出样本量。

## Security & Configuration Tips

- `.env` 已在 `.gitignore` 中, 切勿提交 API Key。
- `build_graph_for_batch()` 每次调用会 `DETACH DELETE` 全库; 改这块时明确是否需要增量。
