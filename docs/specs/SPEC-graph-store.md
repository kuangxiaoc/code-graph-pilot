# Spec: graph-store (Neo4j 访问层 + 连接复用)

**Module id:** `graph-store` (见 `capability-map.md`)

## Objective

把所有 Cypher 访问收进一个模块, 修掉硬伤 #8 (每次查询新建
`GraphDatabase.driver`), 并让图查询可离线测试 —— MCP 服务是长驻进程,
每次工具调用建一次驱动等于每次都付一次 TLS + 路由开销。

## 先定义问题, 再写代码

```
FEATURE: 从 Streamlit / Agent / MCP 三处访问 Neo4j

ON-CALL WILL ASK:
1. 为什么服务跑一小时后连接数涨到几百?      -> 驱动未复用
2. 改一个 Cypher 为什么要翻三个文件?        -> 查询散落在 agent.py / app.py
3. 图查询怎么在没有 Docker 的机器上测?      -> 查询与驱动强耦合, 无法桩替换
```

## 设计

```
src/graph_store.py
  GraphStore              # 协议 (Protocol): run() / callers() / callees() / impact() ...
  Neo4jGraphStore         # 默认实现, 持有单个 GraphDatabase.driver
  get_store()             # 进程级单例, 惰性连接
```

**单例而非每请求新建**: `GraphDatabase.driver` 是线程安全且自带连接池的,
设计上就应该全进程共享一个。旧的 `with GraphDatabase.driver(...) as driver`
写法每查询建/销一个池, 在 MCP 长驻场景下是纯浪费。

**查询全部限定 label**: `MATCH (n {name: $name})` 跨 :Function/:Class 匹配,
同名实体互相串 (硬伤 #3)。所有查询按 uid 或 (label, name) 精确定位。

## Boundaries

- **Always**: 新查询加进 `Neo4jGraphStore`, 不得在 agent.py / app.py 里直接写 Cypher。
- **Never**: 在查询里拼接用户输入 (注入); 一律走参数化, 关系类型用白名单。
- **Ask first**: 新增查询方法 (会扩大协议面, MCP 要逐个决定是否暴露)。

## Success Criteria

1. `agent.py` 与 `app.py` 不再直接 import `GraphDatabase`。
2. 全进程只有一个 driver 实例 (有测试守护)。
3. 查询方法可传入假 store 做离线测试 (有测试守护)。
4. 全部现有测试通过, 行为不变。
