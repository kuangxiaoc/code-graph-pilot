"""测试共享配置: 提供 Neo4j 默认连接串。

没有这一步, 无 .env 环境下 graph_store.get_store() 会拿空 URI,
报 "URI scheme b'' is not supported" —— 一个与真实缺陷无关的噪声失败。
这里给出与 docker-compose.yml 一致的默认值, 连不上时各用例自行
走"检索失败"分支或 skip。
"""

import os

os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "password123")
