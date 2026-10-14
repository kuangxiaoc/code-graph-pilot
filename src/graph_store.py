"""Neo4j 访问层: 进程级驱动复用 + 查询收口。

为什么要有这一层 (三条硬伤一次解决):

1. **连接未复用 (硬伤 #8)**: 旧写法每次查询
   `with GraphDatabase.driver(uri, auth) as driver:` —— 每查询建/销一个
   连接池。MCP 服务是长驻进程, 这样跑一晚上连接数会失控。
   `GraphDatabase.driver` 本身线程安全且自带池化, 全进程共享一个才对。
2. **查询散落**: Cypher 原本分布在 agent.py / app.py 两处, 改一处要翻两个文件。
3. **不可离线测试**: 查询与驱动构造强耦合, 不连数据库就无法测图算法。

所有查询都按 uid 或 (label, name) 定位, 不用 `MATCH (n {name: $name})`
这种跨 label 匹配 (硬伤 #3: 同名 Class/Function 互相串)。
"""

import os
import threading
from typing import Optional, Protocol

from neo4j import GraphDatabase

# 可在查询中拼接的 label / 关系类型白名单。
# Cypher 的关系类型无法参数化, 因此必须白名单校验后再拼。
NODE_LABELS = ("Function", "Method", "Class", "Module", "External")
REL_TYPES = ("CALLS", "DEFINED_IN", "IMPORTS", "INHERITS", "BELONGS_TO")

_SYMBOL_LABELS = "Function|Method|Class"


class GraphStore(Protocol):
    """图访问协议。测试可传假实现, 生产传 Neo4jGraphStore。"""

    def symbol(self, name: str) -> Optional[dict]:
        """按 name 或 uid 后缀定位唯一符号; 未命中或多个命中返回 None / 报错。"""
        ...

    def impact(self, name: str, depth: int = 5) -> dict:
        """双向影响分析: 上游调用者 (传递闭包) + 下游依赖。"""
        ...

    def subgraph(self, limit: int = 100) -> dict:
        """可视化用的节点/边抽样。"""
        ...

    def close(self) -> None:
        ...


class Neo4jGraphStore:
    """Neo4j 实现。持有单个 driver, 进程内复用。"""

    def __init__(self, uri=None, user=None, password=None):
        # 默认值与 docker-compose.yml 一致: 起了容器但没写 .env 也该能用。
        # 否则报 "URI scheme b'' is not supported", 对用户毫无信息量。
        self.uri = uri or os.getenv("NEO4J_URI") or "bolt://localhost:7687"
        self.user = user or os.getenv("NEO4J_USER") or "neo4j"
        self.password = password or os.getenv("NEO4J_PASSWORD") or "password123"
        self._driver = GraphDatabase.driver(
            self.uri, auth=(self.user, self.password)
        )

    def close(self):
        self._driver.close()

    # --- 查询 ---

    def symbol(self, name):
        """定位唯一符号, 返回结构化记录; 失败返回 {'error': ...}。

        与旧实现的差异: 查询限定 label (修硬伤 #3), 且失败信息结构化,
        便于调用方区分"未找到"/"多个匹配"/"连接失败"。
        """
        query = f"""
        MATCH (target)
        WHERE (target:{_SYMBOL_LABELS})
          AND (target.name = $name OR target.uid ENDS WITH $suffix)
        OPTIONAL MATCH (target)-[:CALLS]->(downstream)
        OPTIONAL MATCH (upstream)-[:CALLS]->(target)
        RETURN
            target.uid as uid,
            target.name as name,
            target.module as module,
            target.filepath as filepath,
            target.lineno as lineno,
            labels(target) as labels,
            collect(DISTINCT downstream.uid) as calls,
            collect(DISTINCT upstream.uid) as called_by
        """
        try:
            records = self._driver.execute_query(
                query, name=name, suffix=f":{name}"
            ).records
        except Exception as exc:
            return {"error": f"数据库连接错误: {exc}"}

        if len(records) > 1:
            matches = ", ".join(r["uid"] for r in records)
            return {"error": f"⚠️ 符号名 '{name}' 在图中匹配到多个实体: {matches}。"
                             f"请使用 module:name 形式指定唯一目标。"}
        if not records:
            return {"error": f"⚠️ 在知识图谱中未找到符号 '{name}'。"}

        record = records[0]
        # collect() 无匹配时返回 [None]
        return {
            "uid": record["uid"] or name,
            "name": record["name"],
            "module": record["module"],
            "filepath": record["filepath"],
            "lineno": record.get("lineno"),
            "labels": [l for l in (record["labels"] or []) if l != "Symbol"],
            "calls": [c for c in (record["calls"] or []) if c],
            "called_by": [c for c in (record["called_by"] or []) if c],
        }

    def impact(self, name, depth=5):
        """影响分析: 上游传递闭包 (谁最终会受影响) + 直接下游。

        为什么上游用传递闭包而下游只用直接: "改这个函数会影响谁" 问的是风险面,
        必须传递; "它调用了谁" 是局部事实, 直接依赖已够, 展开反而稀释重点。
        """
        symbol = self.symbol(name)
        if symbol.get("error"):
            return symbol
        uid = symbol["uid"]

        # 注意: Neo4j 不允许参数出现在 MATCH 模式里
        # (`MATCH (start {uid: $uid})` 报 "Parameter maps cannot be used in
        # MATCH patterns"), 因此起点用 WHERE 定位; 深度是受控整数, 可直接拼。
        upstream_query = """
        MATCH path = (upstream)-[:CALLS*1..%d]->(start)
        WHERE start.uid = $uid
        RETURN DISTINCT upstream.uid AS uid, length(path) AS distance
        ORDER BY distance
        """ % int(depth)
        try:
            upstream = [
                {"uid": r["uid"], "distance": r["distance"]}
                for r in self._driver.execute_query(
                    upstream_query, uid=uid, depth=int(depth)
                ).records
            ]
        except Exception as exc:
            return {"error": f"数据库连接错误: {exc}"}

        return {
            "uid": uid,
            "name": symbol["name"],
            "labels": symbol["labels"],
            "module": symbol["module"],
            "filepath": symbol["filepath"],
            "upstream": upstream,
            "downstream": symbol["calls"],
        }

    def subgraph(self, limit=100):
        """抽样节点与边, 供可视化。uid 为节点键 (name 跨模块不唯一)。"""
        query = f"""
        MATCH (n)-[r]->(m)
        RETURN n, r, m
        LIMIT {int(limit)}
        """
        try:
            records = self._driver.execute_query(query).records
        except Exception as exc:
            return {"error": f"数据库连接错误: {exc}", "nodes": [], "edges": []}

        nodes, edges, seen = [], [], set()
        for record in records:
            for node in (record["n"], record["m"]):
                uid = node.get("uid")
                if uid and uid not in seen:
                    seen.add(uid)
                    labels = [l for l in node.labels if l != "Symbol"]
                    nodes.append({
                        "uid": uid,
                        "name": node.get("name"),
                        "module": node.get("module"),
                        "filepath": node.get("filepath"),
                        "labels": labels,
                    })
            edges.append({
                "src": record["n"].get("uid"),
                "rel": record["r"].type,
                "dst": record["m"].get("uid"),
            })

        return {"nodes": nodes, "edges": edges}


# --- 进程级单例 ---
# MCP 服务与 Streamlit 各是一个进程, 各自持有一个 store 即可;
# 单例的意义是"同一进程内不重复建驱动", 不是跨进程共享。
_store = None
_lock = threading.Lock()


def get_store():
    """返回进程级 store; 首次调用时才连接 (惰性, 便于无库环境导入)。"""
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = Neo4jGraphStore()
    return _store


def reset_store():
    """测试用: 丢弃单例, 下一个 get_store() 重新连接。"""
    global _store
    with _lock:
        if _store is not None:
            try:
                _store.close()
            except Exception:
                pass
            _store = None
