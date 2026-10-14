"""codegraph CLI: 不靠 Streamlit 使用本项目。

设计约束 (见 docs/specs/SPEC-delivery-surface.md):
- 结构化数据走 stdout, 进度/告警走 stderr —— `codegraph symbol x | jq` 必须干净
- 永远提供 --json
- 失败时退出码非零, stderr 给可读原因, 不甩 traceback

用法:
    uv run python -m codegraph build benchmarks/fixtures/webapp_py
    uv run python -m codegraph symbol validate_token --json
    uv run python -m codegraph impact validate_token --depth 3
    uv run python -m codegraph hierarchy AuthService
    uv run python -m codegraph analyze "修改 validate_token 会影响谁"
    uv run python -m codegraph report
    uv run python -m codegraph validate
    uv run python -m codegraph mcp
"""

import argparse
import json
import sys
from pathlib import Path

# src/ 加入 path: CLI 常以 `python src/codegraph.py` 或 `python -m codegraph`
# 两种方式启动, 后者需要 src/ 在 sys.path 里才能 import 兄弟模块。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _emit(data, as_json, lines):
    """结构化数据走 stdout, 人类可读文本只在非 json 模式输出。"""
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=list))
    else:
        for line in lines:
            print(line)


def _fail(message, code=1):
    """打印可读错误并返回非零退出码。

    不直接 sys.exit: 让调用方 (测试) 能拿到返回码而不必捕获 SystemExit。
    """
    print(f"错误: {message}", file=sys.stderr)
    return code


def _package_parent(path):
    """上溯到包的父目录, 使单文件增量也能算出正确的模块名。

    传单个文件时不能拿文件自己当基准 —— 那会得到 "validators"
    而不是 "utils.validators", 增量重建后图里出现两个 validate_token
    (实测: symbol validate_token 报"匹配到多个实体")。

    两种包形态都要处理:
    - 有 __init__.py: 沿包结构上溯到最外层包, 再上一级
    - 无 __init__.py (fixture / 普通源码树): 上溯到第一个"自身不含
      .py、但子目录里有 .py"的目录 —— 它就是包根
    """
    p = Path(path).resolve()
    if p.is_dir():
        return p

    cur = p.parent
    if (cur / "__init__.py").exists():
        # 一路向上只要父目录仍是包就继续, 停在最外层包的**父目录**
        # (requests/models.py -> /src, 于是模块名是 requests.models
        # 而不是 src.requests.models)。
        while (cur.parent / "__init__.py").exists() and cur.parent != cur:
            cur = cur.parent
        return cur.parent if cur.parent != cur else cur

    while True:
        parent = cur.parent
        if parent == cur:
            break
        has_py = any(cur.glob("*.py"))
        subdirs = [d for d in cur.iterdir() if d.is_dir()
                   and not d.name.startswith((".", "__"))]
        sub_has_py = any(any(d.rglob("*.py")) for d in subdirs)
        # cur 自身没有 .py 而子目录里有: cur 就是包根
        if not has_py and sub_has_py and subdirs:
            return cur
        cur = parent
    return cur


# --- 子命令 ---

def cmd_build(args):
    from graph_builder import CodeAnalyzer, GraphLoader, module_name_for_path, resolve_graph

    root = Path(args.path)
    if not root.exists():
        return _fail(f"路径不存在: {root}")

    files = sorted(str(p) for p in root.rglob("*.py")) if root.is_dir() else [str(root)]
    files = [f for f in files if "__pycache__" not in f]
    if not files:
        return _fail(f"未找到 .py 文件: {root}")

    # 算模块名的基准目录。传单个文件时不能拿文件自己当 root —— 那会得到
    # 模块名 "validators" 而不是 "utils.validators", 增量重建后图里出现
    # 两个 validate_token (实测: symbol validate_token 报"匹配到多个实体")。
    # 沿目录上溯到最外层不含 __init__.py 的目录, 它就是包的父目录。
    base = _package_parent(root)

    # --cache 走文件 hash 缓存: 未变更文件直接重放上次的解析结果。
    # 默认关闭是刻意的: 小仓库上 hash 的开销与解析相当, 只有"文件多、
    # 改动少"时才划算 (实测 300 文件解析约 25ms, hash 约 4ms)。
    reused = 0
    previous_index = {}
    analyzers, skipped = [], []
    if args.cache:
        import file_cache

        previous_index = file_cache.load_index(str(base))
        todo, reuse_files = file_cache.partition(str(base), files)
        cached = file_cache.load_cached(str(base), reuse_files) or []
        analyzers.extend(cached)
        reused = len(cached)
        files = todo
        if reused:
            print(f"缓存命中 {reused} 个文件, 需重解析 {len(files)} 个",
                  file=sys.stderr)

    print(f"解析 {len(files)} 个文件...", file=sys.stderr)
    for filepath in files:
        try:
            analyzers.append(
                CodeAnalyzer(filepath, module=module_name_for_path(Path(filepath), base))
            )
        except SyntaxError as exc:
            skipped.append({"file": filepath, "reason": f"语法错误: {exc}"})
        except Exception as exc:  # noqa: BLE001 - 单文件失败不应中断整批
            skipped.append({"file": filepath, "reason": str(exc)})

    if not analyzers:
        return _fail("没有文件解析成功")

    if args.cache:
        # 只保存本次真正解析过的文件。缓存重放出来的 CachedAnalyzer 没有
        # source, 若一并保存会写坏 blob (它缺 filepath 对应的真实源码 hash
        # 之外的字段), 且会让索引里出现指向不存在 blob 的条目。
        try:
            fresh = [a for a in analyzers if getattr(a, "source", None)]
            if fresh:
                file_cache.save(str(base), fresh, previous_index=previous_index)
        except Exception:  # noqa: BLE001 - 缓存失败不该让构建失败
            pass

    entities, relations = resolve_graph(analyzers)

    # --keep 走增量: 先删本批文件的旧子图, 再写入新解析结果。
    # 少了删除这一步, 源码里重命名/删除的符号会残留在图里 (含 CALLS 边),
    # 影响分析会报告已不存在的调用者 —— 比全库重建更糟, 因为它看起来是增量的。
    modules = sorted({a.module for a in analyzers})
    try:
        loader = GraphLoader()
        if args.keep:
            dropped = loader.drop_modules(modules)
            print(f"增量模式: 删除 {len(modules)} 个模块的旧子图 ({dropped} 节点)",
                  file=sys.stderr)
        else:
            loader.clean_db()
        loader.load_data(entities, relations)
        loader.close()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"写入 Neo4j 失败 (容器起了吗? docker-compose up -d): {exc}")

    result = {
        "files": len(analyzers),
        "reused_from_cache": reused,
        "skipped": skipped,
        "entities": len(entities),
        "relations": len(relations),
        "incremental": bool(args.keep),
        "modules": modules,
    }
    _emit(
        result,
        args.json,
        [
            f"入库完成: {len(entities)} 节点 / {len(relations)} 关系",
            f"跳过 {len(skipped)} 个文件" if skipped else "无跳过文件",
        ],
    )
    return 0


def cmd_symbol(args):
    from graph_store import get_store

    record = get_store().symbol(args.name)
    if record.get("error"):
        return _fail(record["error"])

    lines = [
        f"{record['uid']}  ({', '.join(record['labels'])})",
        f"  位置: {record['filepath']}:{record['lineno'] or '?'}",
        f"  下游 (它调用): {', '.join(record['calls']) or '无'}",
        f"  上游 (谁调用): {', '.join(record['called_by']) or '无'}",
    ]
    _emit(record, args.json, lines)
    return 0


def cmd_impact(args):
    from graph_store import get_store

    result = get_store().impact(args.name, depth=args.depth)
    if result.get("error"):
        return _fail(result["error"])

    lines = [f"{result['uid']}  ({', '.join(result['labels'])})", "", "上游影响 (传递闭包):"]
    if result["upstream"]:
        for item in result["upstream"]:
            lines.append(f"  [距离 {item['distance']}] {item['uid']}")
    else:
        lines.append("  无 —— 可能是入口函数或未被调用")
    lines += ["", f"下游依赖 (直接): {', '.join(result['downstream']) or '无'}"]

    _emit(result, args.json, lines)
    return 0


def cmd_hierarchy(args):
    from graph_store import get_store

    result = get_store().hierarchy(args.name)
    if result.get("error"):
        return _fail(result["error"])

    lines = [f"{result['uid']}  ({', '.join(result['labels'])})", ""]
    lines.append(f"父类链 (INHERITS): {', '.join(result['ancestors']) or '无'}")
    lines.append(f"子类 (INHERITS):   {', '.join(result['subclasses']) or '无'}")
    lines.append("成员 (BELONGS_TO):")
    if result["members"]:
        for m in result["members"]:
            lines.append(f"  {m}")
    else:
        lines.append("  无")
    # 继承来的方法: 父类的成员也是本类可调用的, 改父类要算进影响面
    if result["ancestors"]:
        lines += ["", "提示: 修改父类的成员方法会影响本类, "
                      "这部分影响面 CALLS 边覆盖不到。"]

    _emit(result, args.json, lines)
    return 0


def cmd_mcp(args):
    """启动 MCP 服务。

    延迟 import: mcp 是可选依赖, 不装也能用其余命令。
    """
    try:
        from mcp_server import main as mcp_main
    except ImportError:
        return _fail(
            "未安装 mcp 依赖。安装方法: uv add mcp\n"
            "  CLI 其余命令不需要它。"
        )

    if not args.json:
        print("启动 MCP 服务 (stdio)...", file=sys.stderr)
    mcp_main()
    return 0


def cmd_analyze(args):
    import agent

    if not agent.has_llm_credentials():
        return _fail(
            "未配置 QWEN_API_KEY, analyze 需要 LLM。\n"
            "  只查结构请用: codegraph impact <name>\n"
            "  配置方法见 README「配置环境」一节。"
        )

    result, trace = agent.run_analysis(args.query, trace_path=args.trace)
    if result.get("retrieval_failed"):
        print("警告: 图谱检索失败 (Neo4j 可达? 图已入库?), 回答不基于图事实。",
              file=sys.stderr)

    payload = {
        "query": args.query,
        "target": result.get("target_func"),
        "response": result.get("response", ""),
        "terminated_by": result.get("terminated_by"),
        "revision_count": result.get("revision_count", 0),
        "nodes_visited": result.get("nodes_visited", []),
        "metrics": trace["metrics"] if trace else None,
        "retrieval_failed": bool(result.get("retrieval_failed")),
    }
    _emit(payload, args.json, [result.get("response", "")])
    return 0


def cmd_report(args):
    from observability.report import format_report, load_traces, summarize

    records = load_traces(args.path)
    if args.last:
        records = records[-args.last:]
    summary = summarize(records)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=list))
    else:
        print(format_report(summary))
    return 0


def cmd_validate(args):
    from ontology.export import graph_to_rdf
    from ontology.schema import build_schema
    from ontology.validate import validate_graph
    from benchmarks.runner import build_graph

    fixture_dir = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / args.fixture
    try:
        graph = build_graph(fixture_dir)
    except FileNotFoundError as exc:
        return _fail(str(exc))

    rdf = graph_to_rdf(
        {"entities": graph["entities"], "relations": graph["relations"]},
        schema=build_schema(),
    )
    report = validate_graph(rdf, entities=graph["entities"])
    payload = report.to_dict()
    payload["triples"] = len(rdf)
    payload["fixture"] = args.fixture

    _emit(
        payload,
        args.json,
        [f"fixture: {args.fixture}", f"triples: {len(rdf)}  violations: {report.count}", "", report.describe()],
    )
    # 违规非零即非零退出码, 可接 CI
    return 0 if report.count == 0 else 1


def main(argv=None):
    """入口。argv 可注入以便测试; 省略时读 sys.argv。"""
    parser = argparse.ArgumentParser(
        prog="codegraph",
        description="代码知识图谱: 建图 / 结构查询 / Agent 分析 / 指标报告",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add(name, help_text, func):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--json", action="store_true", help="输出 JSON (可被 jq 消费)")
        p.set_defaults(func=func)
        return p

    p_build = _add("build", "解析目录并写入 Neo4j", cmd_build)
    p_build.add_argument("path", help="Python 目录或单文件")
    p_build.add_argument("--cache", action="store_true",
                         help="按文件 hash 复用上次解析结果 (文件多、改动少时才有收益)")
    p_build.add_argument("--keep", action="store_true",
                         help="增量模式: 先删除本批文件的旧子图再写入。"
                              "注意只清理本次传入的文件; 其他文件指向"
                              "已删除符号的边需一并重新 build 那些文件")

    p_symbol = _add("symbol", "定位符号并显示直接上下游", cmd_symbol)
    p_symbol.add_argument("name", help="符号名或 module:qualname")

    p_impact = _add("impact", "传递闭包影响分析", cmd_impact)
    p_impact.add_argument("name", help="符号名或 module:qualname")
    p_impact.add_argument("--depth", type=int, default=5, help="上游传递深度 (默认 5)")

    p_hier = _add("hierarchy", "类继承与成员关系 (INHERITS / BELONGS_TO)", cmd_hierarchy)
    p_hier.add_argument("name", help="类名或 module:qualname")

    p_analyze = _add("analyze", "跑完整 Agent 工作流 (需要 LLM Key)", cmd_analyze)
    p_analyze.add_argument("query", help="自然语言问题")
    p_analyze.add_argument("--trace", default=None, help="轨迹输出路径")

    p_report = _add("report", "汇总轨迹指标", cmd_report)
    p_report.add_argument("--last", type=int, default=None, help="只看最近 N 条")
    p_report.add_argument("--path", default=None, help="轨迹 JSONL 路径")

    _add("mcp", "启动 MCP 服务 (stdio, 供外部 agent 调用)", cmd_mcp)

    p_validate = _add("validate", "本体约束校验 (CI 门禁)", cmd_validate)
    p_validate.add_argument("--fixture", default="webapp_py")

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
