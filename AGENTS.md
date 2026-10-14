# Repository Guidelines

**写代码前先读 `CONSTRAINTS.md`。不得为了让某个改动通过而放宽它。**

## Project Structure & Module Organization

```
src/
  graph_builder.py   # ast.NodeVisitor 解析器 + GraphLoader (Neo4j 写入)
  agent.py           # LangGraph 状态机: parse -> retrieve -> generate -> review
  app.py             # Streamlit UI (侧边栏入库 / 对话分析 / 图谱可视化)
test_code.py         # 演示样本: 4 个互相调用的函数, 供手动入库试用
assets/              # README 截图
benchmarks/          # 评测: fixture + 人工 ground truth + 打分 runner
ontology/            # RDFS/OWL schema + SHACL 约束校验
docs/specs/          # 能力地图与各模块 spec
docs/knowledge/      # 领域调研与评测方法笔记
docker-compose.yml   # Neo4j 容器 (7474 HTTP / 7687 bolt)
pyproject.toml       # uv 依赖声明
tests/               # pytest, 全部离线 (不连 Neo4j, 不调 LLM)
CONSTRAINTS.md       # 质量契约, 含可执行阈值
```

模块优先级见 `docs/specs/capability-map.md`。术语统一用 **Code Knowledge Graph (CKG)**,
不要写 CPG——CFG/PDG 尚未实现。

## Build, Test, and Development Commands

```bash
docker-compose up -d          # 启动 Neo4j (neo4j / password123)
uv sync                        # 安装依赖 (Python 3.10, 见 .python-version)
uv run pytest tests/ -q        # 跑测试 (离线, 约 60 秒)
uv run python benchmarks/runner.py   # 跑评测, 打印 recall 表格
uv run streamlit run src/app.py   # 启动前端
```

`.env` 需要提供 `NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD`、`QWEN_API_KEY`、`QWEN_BASE_URL`。

## Coding Style & Naming Conventions

- 4 空格缩进; 函数/变量 `snake_case`, 类 `PascalCase`。
- 注释与文档字符串用**中文**; 面向 LLM 的 prompt 与 Cypher 结果 key 保留英文。
- 无 ruff / black / mypy 配置。新增代码请保持与现有文件一致, 不要顺手全局重格式化。

## Testing Guidelines

pytest 已在 dev 依赖中, 测试放仓库根 `tests/`, 命名 `test_<module>.py`。

**所有测试必须离线**: 不连 Neo4j、不调 LLM、不访问网络。理由是可重复——
`agent.py` 的 LLM 已改为惰性构造, 正是为了这个目的。

解析逻辑直接断言 `CodeAnalyzer(...).entities / .relations`, 不经过加载器。
改动检索质量时同步跑 `benchmarks/runner.py`, 按 `CONSTRAINTS.md` 守住 recall 基线。

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
