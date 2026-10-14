"""AST 解析器 + Neo4j 数据加载器。

图模型（Code Knowledge Graph）:
    (:Module) 每文件一个
    (:Class) (:Function) (:Method) 每个符号一个, 以 module 限定的 uid 唯一标识

关系:
    (:Symbol)-[:DEFINED_IN]->(:Module)
    (:Module)-[:IMPORTS]->(:Module)
    (:Class)-[:INHERITS]->(:Class)
    (:Method)-[:BELONGS_TO]->(:Class)
    (:Symbol)-[:CALLS]->(:Symbol)          仅在本模块内能确定解析时建立

无法确定目标类型的调用 (如 obj.save()) 不建立 CALLS 边, 记录到
analyzer.unresolved_calls, 避免靠 "名字相同" 伪造跨实体依赖。
"""

import ast
import builtins
import os
from pathlib import Path

from neo4j import GraphDatabase

# 允许写入 Neo4j 的节点 label / 关系类型白名单。
NODE_LABELS = {"Module", "Class", "Function", "Method", "External"}
REL_TYPES = {"DEFINED_IN", "IMPORTS", "INHERITS", "BELONGS_TO", "CALLS"}

_BUILTIN_NAMES = set(dir(builtins))


def derive_module_name(filepath):
    """从文件路径推导模块名。

    上传场景下文件被平铺在临时目录, 因此取文件名 stem 作为模块名。
    """
    p = Path(filepath)
    stem = p.stem
    if stem == "__init__":
        return p.parent.name or stem
    return stem


def module_name_for_path(filepath, root):
    """把文件路径映射为相对 root 的点分模块名。

    例如 root=/proj 时 /proj/utils/validators.py -> "utils.validators"。
    与 Python 的 import 写法一致, 否则包内跨文件导入会全部失配。
    """
    p = Path(filepath).resolve()
    root = Path(root).resolve()
    try:
        rel = p.relative_to(root)
    except ValueError:
        # 不在 root 下时退回 stem 策略 (上传临时目录场景)
        return derive_module_name(filepath)

    parts = list(rel.parts)
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = Path(parts[-1]).stem
    return ".".join(parts) if parts else derive_module_name(filepath)


class CodeAnalyzer(ast.NodeVisitor):
    """把单个 Python 源文件解析为带 module 限定的实体与关系。"""

    def __init__(self, source, module=None, filepath=None):
        # 兼容两种用法: 传源码字符串, 或传文件路径。
        # 传路径时无条件读取文件; 显式传入的 module 优先于路径推导。
        if isinstance(source, str) and os.path.isfile(source):
            filepath = filepath or str(source)
            if module is None:
                module = derive_module_name(filepath)
            with open(source, "r", encoding="utf-8") as f:
                source = f.read()

        self.filepath = filepath
        self.module = module or "<unknown>"
        self.source = source

        self.entities = []
        self.relations = []
        self.unresolved_calls = []
        self.external_relations = []  # 跨模块边, 待 resolve_graph 判定目标是否存在

        # 解析期状态
        self._scope = []             # [(kind, name)], kind in {"class", "func"}
        self._raw_calls = []         # 待解析的调用
        self._local_classes = {}     # 简单类名 -> uid
        self._local_functions = {}   # 简单函数名 -> uid
        self._local_methods = {}     # (类名, 方法名) -> uid
        self._import_aliases = {}    # 本地名 -> (来源模块, 原名)
        self._seen_rel = set()       # 关系去重

        tree = ast.parse(source)
        self._module_uid = self.module
        self.entities.append({
            "type": "Module",
            "name": self.module,
            "uid": self._module_uid,
            "module": self.module,
            "filepath": self.filepath,
            "lineno": 1,
            "external": False,
            "kind": "module",
        })

        self.visit(tree)                 # 收集定义
        self._resolve_calls()            # 用品名表解析调用
        for entity in self.entities:     # DEFINED_IN
            if entity.get("external") or entity["type"] == "Module":
                continue
            self._add_relation(entity["uid"], "DEFINED_IN", self._module_uid)

    # ---------- 内部工具 ----------
    def add_cross_module_edge(self, src, rel, dst, **extra):
        """记录一条目标可能在别处定义的边, 交由 resolve_graph 判定。

        本地能确定的目标请直接用 _add_relation。
        """
        self.external_relations.append({"src": src, "rel": rel, "dst": dst, **extra})

    def _add_relation(self, src, rel, dst, **extra):
        key = (src, rel, dst)
        if key in self._seen_rel:
            return
        self._seen_rel.add(key)
        record = {"src": src, "rel": rel, "dst": dst}
        record.update(extra)
        self.relations.append(record)

    def _qualname(self, name):
        return ".".join([n for _, n in self._scope] + [name])

    def _uid(self, qualname):
        return f"{self.module}:{qualname}"

    def _current_class(self):
        for kind, name in reversed(self._scope):
            if kind == "class":
                return name
        return None

    def _current_function_uid(self):
        for idx in range(len(self._scope) - 1, -1, -1):
            if self._scope[idx][0] == "func":
                qualname = ".".join(n for _, n in self._scope[: idx + 1])
                return self._uid(qualname)
        return None

    # ---------- 定义收集 ----------
    def visit_ClassDef(self, node):
        qualname = self._qualname(node.name)
        uid = self._uid(qualname)
        self._local_classes[node.name] = uid
        self.entities.append({
            "type": "Class",
            "name": qualname,
            "uid": uid,
            "module": self.module,
            "filepath": self.filepath,
            "lineno": node.lineno,
            "external": False,
            "kind": "class",
        })

        for base in node.bases:
            if isinstance(base, ast.Name):
                if base.id in self._local_classes:
                    self._add_relation(uid, "INHERITS", self._local_classes[base.id],
                                       dst_kind="Class")
                elif base.id in self._import_aliases:
                    # 基类由其他模块导入, 解析成 module:Name 交由 resolve_graph
                    src_module, orig = self._import_aliases[base.id]
                    self.add_cross_module_edge(uid, "INHERITS", f"{src_module}:{orig or base.id}",
                                               dst_kind="Unknown")
                else:
                    # 基类可能来自其他模块或外部库, 留给 resolve_graph
                    self.add_cross_module_edge(uid, "INHERITS", base.id, dst_kind="Unknown")

        self._scope.append(("class", node.name))
        self.generic_visit(node)
        self._scope.pop()

    def _visit_function(self, node):
        parent_is_class = bool(self._scope) and self._scope[-1][0] == "class"
        qualname = self._qualname(node.name)
        uid = self._uid(qualname)
        node_type = "Method" if parent_is_class else "Function"

        if parent_is_class:
            self._local_methods[(self._scope[-1][1], node.name)] = uid
        else:
            self._local_functions[node.name] = uid

        self.entities.append({
            "type": node_type,
            "name": qualname,
            "uid": uid,
            "module": self.module,
            "filepath": self.filepath,
            "lineno": node.lineno,
            "external": False,
            "kind": "method" if parent_is_class else "function",
        })

        if parent_is_class:
            self._add_relation(uid, "BELONGS_TO", self._local_classes[self._scope[-1][1]])

        self._scope.append(("func", node.name))
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)

    def visit_Import(self, node):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self._import_aliases[local] = (alias.name, None)
            self.add_cross_module_edge(self._module_uid, "IMPORTS", alias.name)
            self._register_external_module(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        base = "." * node.level + (node.module or "")
        for alias in node.names:
            local = alias.asname or alias.name
            self._import_aliases[local] = (base, alias.name)
            self.add_cross_module_edge(self._module_uid, "IMPORTS", base)
            self._register_external_module(base)
        self.generic_visit(node)

    def _register_external_module(self, module_name):
        if not module_name or module_name == self.module:
            return
        if any(e["uid"] == module_name for e in self.entities):
            return
        self.entities.append({
            "type": "External",
            "name": module_name,
            "uid": module_name,
            "module": module_name,
            "filepath": None,
            "lineno": None,
            "external": True,
            "kind": "module",
        })

    # ---------- 调用收集 ----------
    def visit_Call(self, node):
        caller = self._current_function_uid()
        if caller is not None:
            # 类上下文必须在遍历期捕获: 解析期 scope 栈已清空。
            self._raw_calls.append((caller, self._current_class(), node.func))
        self.generic_visit(node)

    def _resolve_calls(self):
        for caller, owner_class, func in self._raw_calls:
            target = self._resolve_target(func, owner_class)
            if target is None:
                name = self._call_display_name(func)
                if name and name not in _BUILTIN_NAMES:
                    self.unresolved_calls.append({"caller": caller, "name": name})
                continue
            # 本地目标直接连边; 跨模块目标留给 resolve_graph 判定
            # (对方可能在本批文件中定义, 也可能是外部库, 或根本不存在)。
            if target.startswith(f"{self.module}:"):
                self._add_relation(caller, "CALLS", target)
            else:
                self.add_cross_module_edge(caller, "CALLS", target)

    def _resolve_target(self, func, owner_class=None):
        """把调用表达式解析成目标 uid; 无法确定时返回 None。"""
        # getattr(...)() 之类的动态调用: 不猜
        if isinstance(func, ast.Call):
            return None

        if isinstance(func, ast.Name):
            name = func.id
            if name in self._local_functions:
                return self._local_functions[name]
            if name in self._import_aliases:
                src_module, orig = self._import_aliases[name]
                return f"{src_module}:{orig or name}"
            if name in self._local_classes:
                return self._local_classes[name]
            return None

        if isinstance(func, ast.Attribute):
            attr = func.attr
            value = func.value

            # self.method() -> 当前类的方法
            if isinstance(value, ast.Name) and value.id == "self":
                if owner_class and (owner_class, attr) in self._local_methods:
                    return self._local_methods[(owner_class, attr)]
                return None

            # Class.method() -> 本模块内的类方法
            if isinstance(value, ast.Name) and value.id in self._local_classes:
                return self._local_methods.get((value.id, attr))

            # module.func() -> 已导入模块
            if isinstance(value, ast.Name) and value.id in self._import_aliases:
                src_module, _ = self._import_aliases[value.id]
                return f"{src_module}:{attr}"

            # obj.method(): 对象类型未知, 不伪造
            return None

        return None

    @staticmethod
    def _call_display_name(func):
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None


def resolve_graph(analyzers):
    """把多个文件的解析结果合并成一张可加载的图。

    两阶段: 先按 uid 归并所有节点, 再判定每条跨模块调用是否有真实目标。
    目标不存在 (外部库/未提供源码) 时创建 :External 桩节点, 保证边不悬空、
    也不会像旧实现那样被 Neo4j 的 MATCH 静默丢弃。

    返回 (entities, relations)。
    """
    entities = {}
    relations = []
    seen_rel = set()

    def add_entity(entity):
        uid = entity["uid"]
        # 真实定义的节点优先于 External 桩
        if uid in entities and entities[uid]["type"] == "External" and entity["type"] != "External":
            entities[uid] = entity
        elif uid not in entities:
            entities[uid] = entity

    def add_relation(src, rel, dst, **extra):
        key = (src, rel, dst)
        if key in seen_rel:
            return
        seen_rel.add(key)
        record = {"src": src, "rel": rel, "dst": dst}
        record.update(extra)
        relations.append(record)

    # 1. 归并节点
    for analyzer in analyzers:
        for entity in analyzer.entities:
            add_entity(entity)

    # 2. 归并已确定的边
    for analyzer in analyzers:
        for rel in analyzer.relations:
            add_relation(rel["src"], rel["rel"], rel["dst"],
                         **{k: v for k, v in rel.items() if k not in ("src", "rel", "dst")})

    # 3. 判定所有跨模块边: 目标存在则连通, 否则建 :External 桩
    for analyzer in analyzers:
        for rel in analyzer.external_relations:
            dst = rel["dst"]
            if dst not in entities:
                # 带 "module:symbol" 形式的是符号桩, 裸名是外部模块桩
                is_symbol = ":" in dst
                add_entity({
                    "type": "External",
                    "name": dst,
                    "uid": dst,
                    "module": dst.split(":", 1)[0] if is_symbol else dst,
                    "filepath": None,
                    "lineno": None,
                    "external": True,
                    "kind": "symbol" if is_symbol else "module",
                })
            add_relation(rel["src"], rel["rel"], dst,
                         **{k: v for k, v in rel.items() if k not in ("src", "rel", "dst")})

    return list(entities.values()), relations


class GraphLoader:
    """把 CodeAnalyzer 的输出写入 Neo4j。"""

    def __init__(self, uri=None, user=None, pwd=None):
        # 默认值与 docker-compose.yml 一致, 使"起了容器没写 .env"也能直接用。
        self.driver = GraphDatabase.driver(
            uri or os.getenv("NEO4J_URI") or "bolt://localhost:7687",
            auth=(user or os.getenv("NEO4J_USER") or "neo4j",
                  pwd or os.getenv("NEO4J_PASSWORD") or "password123"),
        )

    def close(self):
        self.driver.close()

    def clean_db(self):
        """清空数据库 (开发测试用)"""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()

    def load_data(self, entities, relations):
        # 先校验再连库: 非法 label / 悬空边必须在写库前暴露
        known = set()
        for entity in entities:
            if entity["type"] not in NODE_LABELS:
                raise ValueError(f"未知节点类型: {entity['type']}")
            known.add(entity["uid"])
        for rel in relations:
            if rel["rel"] not in REL_TYPES:
                raise ValueError(f"未知关系类型: {rel['rel']}")
            # 悬空边必须显式报错, 否则 MATCH 会静默丢边 (旧 :REL_TYPE bug 的同类问题)
            for endpoint in (rel["src"], rel["dst"]):
                if endpoint not in known:
                    raise ValueError(f"关系端点不存在于节点集合: {endpoint}")

        with self.driver.session() as session:
            # 1. 节点: 以 uid 为唯一键, 避免同名实体互相覆盖
            for entity in entities:
                label = entity["type"]
                session.run(
                    f"""
                    MERGE (e:{label} {{uid: $uid}})
                    SET e.name = $name,
                        e.module = $module,
                        e.filepath = $filepath,
                        e.lineno = $lineno,
                        e.external = $external
                    """,
                    uid=entity["uid"],
                    name=entity["name"],
                    module=entity.get("module"),
                    filepath=entity.get("filepath"),
                    lineno=entity.get("lineno"),
                    external=bool(entity.get("external", False)),
                ).consume()

            # 2. 关系: 关系类型无法参数化, 因此用白名单校验后拼接
            for rel in relations:
                session.run(
                    f"""
                    MATCH (src {{uid: $src}})
                    MATCH (dst {{uid: $dst}})
                    MERGE (src)-[:{rel['rel']}]->(dst)
                    """,
                    src=rel["src"],
                    dst=rel["dst"],
                ).consume()


def build_graph_from_file(filepath, uri, user, pwd):
    """单文件导入: 解析 -> 清库 -> 入库。返回 (节点数, 关系数)。"""
    analyzer = CodeAnalyzer(filepath)
    entities, relations = resolve_graph([analyzer])

    loader = GraphLoader(uri, user, pwd)
    # 演示方便: 每次构建都清空图库。如需保留请注释掉 loader.clean_db()
    loader.clean_db()
    loader.load_data(entities, relations)
    loader.close()

    return len(entities), len(relations)


def build_graph_for_batch(file_paths, uri, user, pwd):
    """批量导入: 只在开始时清空一次, 之后按文件增量写入。"""
    loader = GraphLoader(uri, user, pwd)

    print("🧹 Cleaning Database for batch import...")
    loader.clean_db()

    analyzers = []
    for filepath in file_paths:
        try:
            print(f"Analyzing: {filepath}")
            analyzer = CodeAnalyzer(filepath)
            analyzers.append(analyzer)
            if analyzer.unresolved_calls:
                print(f"  ⚠️ {len(analyzer.unresolved_calls)} unresolved call(s)")
        except SyntaxError as e:
            print(f"⚠️ Skipped (syntax error) {filepath}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 - 单个文件失败不应中断整批
            print(f"⚠️ Error parsing {filepath}: {e}")
            continue

    # 所有文件解析完再统一解析跨文件调用, 才能看到全量定义
    entities, relations = resolve_graph(analyzers)
    loader.load_data(entities, relations)
    loader.close()
    return len(entities), len(relations)
