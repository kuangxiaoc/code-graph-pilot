"""MCP 入口: 让代码知识图谱可被外部 agent 调用。

为什么现在做 (此前 spec 明写"条件触发, 现在不做"):
两个触发条件都已满足 ——
1. 真实 LLM 跑通 >=30 次 analyze, 幻觉率有基线数字 (3.6%, n=30)
2. 有明确的外部 agent 平台接入需求

工具面刻意收敛。竞品 codegraph (73k star) 把 7 个工具默认全部不暴露,
只留一个 codegraph_explore, 理由原文是"一个强工具比一堆窄工具更能
引导 agent"。本项目同理: 只暴露 codegraph_query 一个工具, 用 action
参数分派, 并在 description 里写清每个 action 的适用场景与代价。

用法:
    uv run python -m codegraph mcp          # stdio (Cursor/Claude Desktop)
    uv run python -m mcp_server             # 直接跑
"""

import json

from mcp.server.mcpserver import MCPServer

import graph_store

server = MCPServer(
    name="code-graph-pilot",
    description=(
        "代码知识图谱 (Code Knowledge Graph)。基于 AST 解析 + Neo4j, "
        "回答代码影响面问题 (改这个函数会影响谁) —— 结论来自图查询, "
        "不靠 LLM 猜测。"
    ),
    instructions=(
        "用 codegraph_query 查询代码结构。action 选择:\n"
        "- symbol:    定位单个符号的直接上下游 (最快, 先试这个)\n"
        "- impact:    传递闭包影响面, depth 越大越慢\n"
        "- hierarchy: 类继承与成员 (父类方法的影响面 CALLS 覆盖不到)\n"
        "- overview:  图里有什么 (不确定符号名时先调这个)\n"
        "所有查询都是确定性的: 结果直接来自图, 不是模型生成的。\n"
        "图没建过时会返回提示, 此时先跑 codegraph build。"
    ),
    version="0.1.0",
)

_ACTIONS = ("symbol", "impact", "hierarchy", "overview")

_TOOL_DESC = (
    "查询代码知识图谱。action:\n"
    "  symbol    - 定位符号, 返回直接上游(谁调用它)与下游(它调用谁)\n"
    "  impact    - 传递闭包影响面 (改它会波及谁), depth 默认 5\n"
    "  hierarchy - 类的父类链、子类、成员方法\n"
    "  overview  - 图概览: 有哪些模块与符号, 不确定名字时先调这个\n"
    "name 支持裸名 (validate_token) 或 module:qualname "
    "(utils.validators:validate_token)。同名跨模块时会提示用限定名。"
)


@server.tool(description=_TOOL_DESC)
def codegraph_query(action: str, name: str = "", depth: int = 5) -> str:
    """确定性图查询。返回 JSON 字符串。"""
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return _err("action 必须是 %s 之一" % " / ".join(_ACTIONS))

    try:
        store = graph_store.get_store()
    except Exception as exc:  # noqa: BLE001 - MCP 里不能抛, 要返回可读串
        return _err("无法连接图数据库: %s" % exc)

    try:
        if action == "overview":
            return _ok(store.subgraph(limit=100))
        if not name.strip():
            return _err("action=%s 需要提供 name 参数" % action)
        if action == "symbol":
            return _ok(store.symbol(name.strip()))
        if action == "impact":
            return _ok(store.impact(name.strip(), depth=max(1, min(depth, 10))))
        return _ok(store.hierarchy(name.strip()))
    except Exception as exc:  # noqa: BLE001
        return _err("查询失败: %s" % exc)


def _ok(payload):
    """成功时原样返回 store 的结构化结果 (含 error 字段也照原样)。

    不在这里把 error 改写成异常: store 的 {"error": ...} 是**预期内的
    查询结果** (符号不存在 / 匹配到多个), agent 需要据此决定下一步,
    而不是看到一个抛错就放弃。
    """
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(message):
    return json.dumps({"error": message}, ensure_ascii=False)


def main():
    server.run()


if __name__ == "__main__":
    main()
