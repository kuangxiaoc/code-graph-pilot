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

# 不携带类型信息的泛标注。当类型用会造出 `typing:Any.foo` 这种假符号。
_WILDCARD_ANNOTATIONS = {"Any", "object", "Optional", "Union"}


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
    if not parts:
        # root 与 filepath 是同一个文件时 relative_to 得到空路径。
        # 单文件增量重建 (codegraph build x.py --keep) 走的正是这条路,
        # 此前会 IndexError 进而报"没有文件解析成功"。
        return derive_module_name(filepath)
    if parts[-1] == "__init__.py":
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
        self._local_var_types = {}   # (函数 uid, 变量名) -> 类型的 uid 或别名
        self._pending_var_calls = [] # 跨模块变量类型调用, 待 resolve_graph 判定
        self._pending_assigns = []   # 待推断类型的赋值 (调用者 uid, 变量名, 右侧 AST, 类名)
        self._method_returns = {}    # (类简单名, 方法名) -> 返回标注 AST
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
        self._build_class_index()        # 类索引 + 继承表 (推断与解析都要用)
        self._infer_pending_var_types()  # 变量类型推断 (此时签名表才完整)
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

        # 参数类型标注也是可靠的类型来源: `def f(self, r: Response)` 之后
        # `r.headers` / `r.content` 就能解析到真实符号。真实仓库 (requests)
        # 里大量调用走的是参数而不是局部赋值 —— 只靠 visit_Assign 时
        # 303 个 unresolved 里有相当一部分属于这一类。
        self._register_param_types(uid, node)
        if parent_is_class and node.returns is not None:
            # 返回标注供 `conn = self.get_conn()` 这类赋值推断变量类型。
            # 存 AST 而非 uid: 类表在遍历期才逐步填满, 提前转 uid 会漏
            # (例如 send 定义在 get_conn 之前时, 类已入表但标注还没读到)。
            self._method_returns[(self._scope[-1][1], node.name)] = node.returns

        self._scope.append(("func", node.name))
        self.generic_visit(node)
        self._scope.pop()

    def _register_param_types(self, func_uid, node):
        """把带类型标注的参数记进变量类型表。

        只认简单标注 (`r: Response`)。`X | None`、`Optional[X]`、
        `list[str]` 这类复合标注不猜: 取其中一个分支会连错边, 而连错比
        unresolved 更糟 (它会让影响分析指向错误的符号)。
        """
        args = node.args
        pairs = list(getattr(args, "posonlyargs", []) or []) + list(args.args)
        pairs += list(args.kwonlyargs or [])
        for arg in pairs:
            ann = arg.annotation
            if ann is None:
                continue
            type_uid = self._annotation_to_uid(ann)
            if type_uid is None:
                continue
            key = (func_uid, arg.arg)
            # 局部赋值优先: 函数内 `r = something` 会覆盖标注
            if key not in self._local_var_types:
                self._local_var_types[key] = type_uid

    def _annotation_to_uid(self, ann):
        """标注 AST -> 类型 uid; 复合/无法确定的返回 None。"""
        # `X | None` / `X | Y` (Python 3.10+): 不猜是哪个分支
        if isinstance(ann, ast.BinOp):
            return None
        if isinstance(ann, ast.Name):
            name = ann.id
            # Any / object / Optional 之外的泛标注不携带类型信息。此前
            # `resp: Any` 会把变量类型记成 typing:Any, 之后
            # resp.connection_from_url() 造出 typing:Any.connection_from_url
            # 这种假符号桩 (实测 requests.adapters 里两处)。
            if name in _WILDCARD_ANNOTATIONS:
                return None
            if name in self._local_classes:
                return self._local_classes[name]
            if name in self._import_aliases:
                src_module, orig = self._import_aliases[name]
                return f"{src_module}:{orig or name}"
            return None
        # module.Class 形态
        if isinstance(ann, ast.Attribute):
            if isinstance(ann.value, ast.Name) and ann.value.id in self._import_aliases:
                src_module, _ = self._import_aliases[ann.value.id]
                return f"{src_module}:{ann.attr}"
            return None
        # 下标 (list[str] / Dict[K,V]) 与字符串标注 ("Response") 都不猜
        return None

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

    def _resolve_relative(self, level, module):
        """把相对导入解析成绝对模块名。

        `from .models import Response` 在 requests.auth 里实际是
        requests.models。此前直接拼成 ".models", 于是所有经相对导入的
        类型标注都连到了不存在的模块 —— 真实仓库里这是主要形态
        (requests 内部全用相对导入)。
        """
        if not level:
            return module or ""
        parts = self.module.split(".")
        # 当前文件是 __init__.py 时, 它本身就代表包 —— level=1 指向自身;
        # 否则 level=1 指向父包。混淆这两种情形会让 requests/__init__.py
        # 里的 `from .models import` 解析成 "models" 而非 "requests.models"。
        is_package = (self.filepath or "").endswith("__init__.py")
        up = level - 1 if is_package else level
        prefix = parts[: len(parts) - up] if up < len(parts) else []
        tail = module.split(".") if module else []
        return ".".join([p for p in prefix if p] + tail)

    def visit_ImportFrom(self, node):
        base = self._resolve_relative(node.level, node.module)
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

    # ---------- 局部变量类型推断 ----------
    def visit_Assign(self, node):
        """记录 `x = ClassName()` 这类赋值, 供 obj.method() 解析。

        为什么需要: 此前 obj.method() 一律记 unresolved, 于是
        `repo.save()` / `svc.refresh()` 这类最常见的调用形态全部丢失 ——
        benchmark 的 11_deep_callees 场景 recall 只有 0.545, 遗漏的 5 个
        符号全部是这个原因。

        为什么只在函数内做: 模块级赋值的生命周期跨函数, 推断错了会把
        不同函数的调用串到一起。函数内赋值的作用域是确定的。
        """
        caller = self._current_function_uid()
        if caller is not None:
            # 先按当时可见的信息试一次 (链式赋值/已登记的类型), 推不出就
            # 记入待推断队列 —— `x = self.get_conn()` 的返回标注可能写在
            # 文件后部, 遍历到这里时还没登记。
            type_uid = self._infer_type(node.value)
            if type_uid is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._local_var_types[(caller, target.id)] = type_uid
            else:
                # 类名必须在遍历期捕获: _infer_type 现在不依赖 scope 栈,
                # 但 self.method() 的返回标注查找需要知道调用点属于哪个类。
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._pending_assigns.append(
                            (caller, target.id, node.value, self._current_class()))
        self.generic_visit(node)

    def _infer_pending_var_types(self):
        """遍历结束后再试一次类型推断 (此时签名表才完整)。

        只处理 `self.method()` 这一种新形态: 它的返回标注可能在文件后部
        才定义。此时 visit 已结束, scope 栈为空, 因此类名直接取自
        赋值右侧的方法名所属类 —— 用 _current_class() 会在解析期之后失效,
        改由调用点在登记时带上。
        """
        for caller, var, value, owner in self._pending_assigns:
            type_uid = self._infer_type(value, owner)
            if type_uid is not None:
                self._local_var_types[(caller, var)] = type_uid
        self._pending_assigns = []

    def _infer_type(self, value, owner=None):
        """从赋值右侧推断类型 uid; 推断不出返回 None。

        认三种可靠形态:
        - `ClassName()`: 本模块类或已导入的类
        - `x`: 已记录类型的另一个局部变量 (链式赋值)
        - `self.method()`: 该方法有 `-> Type` 返回标注时, 取该类型

        为什么认 `self.method()`: requests 的 adapters.py:696
        `conn = self.get_connection_with_tls_context(...)` 之后
        `conn.urlopen()` 是最典型的调用链, 没有它整条链断在赋值处
        (实测 17 条真漏检里 4 条属于这一类)。只在标注存在时推断:
        没有标注的方法可能返回任何东西, 猜会连错边。
        """
        if not isinstance(value, ast.Call):
            # 链式赋值 y = x: 沿用已知类型
            if isinstance(value, ast.Name):
                caller = self._current_function_uid()
                return self._local_var_types.get((caller, value.id))
            return None

        func = value.func
        if isinstance(func, ast.Name):
            if func.id in self._local_classes:
                return self._local_classes[func.id]
            if func.id in self._import_aliases:
                src_module, orig = self._import_aliases[func.id]
                # 导入的是函数而非类 (cast / urlparse / get_logger) 时,
                # 它不是类型。此前把 `cookie_jar = cast(...)` 记成
                # 类型 typing:cast, 后续 cookie_jar.set_cookie() 会连到
                # typing:cast.set_cookie 这种不存在的符号。
                if orig and orig[0].isupper():
                    return f"{src_module}:{orig}"
                if not orig and func.id[0].isupper():
                    # `from re import compile` 这种: 小写名是函数,
                    # 调用结果是实例, 类型未知
                    return None
                # orig 为 None 表示 `import hashlib` —— func.id 是模块名,
                # 调用 hashlib.sha256() 得到的是实例而非类, 类型未知。
                return None
        elif isinstance(func, ast.Attribute):
            # module.Class() 形态
            if isinstance(func.value, ast.Name) and func.value.id in self._import_aliases:
                src_module, _ = self._import_aliases[func.value.id]
                return f"{src_module}:{func.attr}"
            # self.method(): 取该方法的返回标注。沿继承链向上找 ——
            # 子类调用父类的构造/工厂方法是常态。
            if (isinstance(func.value, ast.Name) and func.value.id == "self"
                    and owner is not None):
                for cls in self._class_chain(owner):
                    ann = self._method_returns.get((cls, func.attr))
                    if ann is not None:
                        return self._annotation_to_uid(ann)
        return None

    def _class_chain(self, class_name, _depth=0):
        """类名 -> 自身及所有本模块祖先的简单类名 (防循环继承)。"""
        if _depth > 10:
            return []
        chain = [class_name]
        for parent in self._bases_by_class.get(class_name, []):
            for anc in self._class_chain(parent, _depth + 1):
                if anc not in chain:
                    chain.append(anc)
        return chain

    # ---------- 调用收集 ----------
    def visit_Call(self, node):
        caller = self._current_function_uid()
        if caller is not None:
            # 类上下文必须在遍历期捕获: 解析期 scope 栈已清空。
            self._raw_calls.append((caller, self._current_class(), node.func))
        self.generic_visit(node)

    def _resolve_calls(self):
        # 类索引已在 __init__ 末尾建好, 这里不再重复。
        for caller, owner_class, func in self._raw_calls:
            targets = self._resolve_targets(func, owner_class, caller)
            if not targets:
                name = self._call_display_name(func)
                if name and name not in _BUILTIN_NAMES:
                    self.unresolved_calls.append({"caller": caller, "name": name})
                continue
            for target in targets:
                # 本地目标直接连边; 跨模块目标留给 resolve_graph 判定
                # (对方可能在本批文件中定义, 也可能是外部库, 或根本不存在)。
                if target.startswith(f"{self.module}:"):
                    self._add_relation(caller, "CALLS", target)
                else:
                    self.add_cross_module_edge(caller, "CALLS", target)

    def _resolve_targets(self, func, owner_class=None, caller_ctx=None):
        """_resolve_target 的多目标版本: 加一条多态分派边。

        只在"静态类型是本模块基类、且本模块有子类重写该方法"时追加
        子类实现 —— `adapter.send()` 连到 BaseAdapter.send 之外再连
        HTTPAdapter.send。没有重写时行为与单目标完全一致, 因此既不影响
        fixture recall, 也不会凭空造边。
        """
        target = self._resolve_target(func, owner_class, caller_ctx)
        if target is None:
            return []
        out = [target]
        # 只对 `变量.方法()` 形态追加: 静态类型才可能声明成基类。
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id != "self"):
            var_type = self._local_var_types.get((caller_ctx, func.value.id))
            if (var_type in self._class_by_uid
                    and var_type.startswith(f"{self.module}:")):
                cls = self._class_by_uid[var_type]
                for override in self._overriding_methods(cls, func.attr):
                    if override not in out:
                        out.append(override)
        return out

    def _build_class_index(self):
        """建 类 uid -> 简单类名 的反查表, 并汇总继承关系。

        _resolve_target 拿到的是类 uid (如 auth.service:Repository),
        但 _local_methods 用简单类名索引, 因此需要反查。
        """
        self._class_by_uid = {
            e["uid"]: e["name"].split(".")[-1]
            for e in self.entities if e["type"] == "Class"
        }
        # 简单类名 -> 父类简单类名 (只保留本模块内的, 跨模块由 resolve_graph 处理)
        self._bases_by_class = {}
        for r in self.relations:
            if r["rel"] == "INHERITS" and r.get("dst_kind") == "Class":
                child = r["src"].split(":")[-1].split(".")[-1]
                parent = r["dst"].split(":")[-1].split(".")[-1]
                self._bases_by_class.setdefault(child, []).append(parent)

    def _lookup_method(self, class_name, attr, _depth=0):
        """在类及其祖先里找方法; 找不到返回 None。

        必须沿继承链向上找: 子类调用继承来的方法是常态, 只在子类自身查
        会漏掉绝大多数 (fixture 里 AuthService 的 login 就继承自 BaseService)。
        """
        if _depth > 10:  # 防循环继承
            return None
        hit = self._local_methods.get((class_name, attr))
        if hit:
            return hit
        for parent in self._bases_by_class.get(class_name, []):
            found = self._lookup_method(parent, attr, _depth + 1)
            if found:
                return found
        return None

    def _overriding_methods(self, class_name, attr, _depth=0):
        """找出本模块内**重写**了该类该方法的所有子类实现。

        为什么需要: `adapter = self.get_adapter(url)` 的静态类型是
        BaseAdapter, 于是 adapter.send() 只连到 BaseAdapter.send ——
        但 HTTPAdapter.send 才是真正被执行的实现。实测 MCP 端到端里
        "改 HTTPAdapter.send 会影响谁" 返回空上游, 而它恰是 requests
        里最常被改的方法。静态类型只说明"签名兼容", 多态分派说明"实际执行谁"。

        返回空列表表示没有子类重写 —— 调用方照旧只连基类方法。
        """
        if _depth > 10:  # 防循环继承
            return []
        found = []
        # _bases_by_class 是 类名 -> 父类名表; 反查"谁继承 class_name"
        # 时也要覆盖间接继承, 否则三层继承链会漏掉中间那层。
        for child in self._bases_by_class:
            if not self._is_descendant(child, class_name):
                continue
            hit = self._local_methods.get((child, attr))
            if hit:
                found.append(hit)
        return found

    def _is_descendant(self, cls, ancestor, _depth=0):
        """cls 是否（间接）继承自 ancestor。"""
        if _depth > 10:
            return False
        for parent in self._bases_by_class.get(cls, []):
            if parent == ancestor:
                return True
            if self._is_descendant(parent, ancestor, _depth + 1):
                return True
        return False

    def _resolve_target(self, func, owner_class=None, caller_ctx=None):
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

            # self.method() -> 当前类的方法 (沿继承链找, 不能只查本类)
            if isinstance(value, ast.Name) and value.id == "self":
                if owner_class:
                    return self._lookup_method(owner_class, attr)
                return None

            # super().method() -> 跳过当前类, 从父类开始找。requests 里
            # 4 个 __init__ 和 CookieJar.set_cookie/__iter__/__contains__
            # 全是这个形态; 不解析时它们全部留在 unresolved。
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id == "super" and owner_class):
                for parent in self._bases_by_class.get(owner_class, []):
                    hit = self._lookup_method(parent, attr)
                    if hit:
                        return hit
                return None

            # Class.method() -> 本模块内的类方法 (沿继承链找)
            if isinstance(value, ast.Name) and value.id in self._local_classes:
                return self._lookup_method(value.id, attr)

            # module.func() -> 已导入模块
            if isinstance(value, ast.Name) and value.id in self._import_aliases:
                src_module, _ = self._import_aliases[value.id]
                return f"{src_module}:{attr}"

            # obj.method(): 查该变量被赋的已知类型
            if isinstance(value, ast.Name):
                var_type = self._local_var_types.get((caller_ctx, value.id))
                if var_type is None:
                    return None
                # 类型在本模块内: 查该类的方法 (含继承来的)
                if var_type in self._class_by_uid:
                    cls = self._class_by_uid[var_type]
                    return self._lookup_method(cls, attr)
                # 类型在别的模块: 该类的真实定义可能在别的 analyzer 里,
                # 此处无法查它的方法表。交给 resolve_graph —— 它看得见全部
                # 节点, 能沿 BELONGS_TO/INHERITS 找到真实方法。
                # 之前这里直接拼 f"{var_type}.{attr}", 而 AuthService.audit
                # 实际继承自 BaseService, 于是凭空造出一个 External 桩。
                self._pending_var_calls.append((caller_ctx, var_type, attr))
                return None

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

    # 3. 第二遍解析: 跨模块的 obj.method() 调用
    #
    # 单文件解析时 `svc = AuthService(); svc.audit()` 无法判定 audit 属于
    # 哪个类 (AuthService 定义在别的文件, 且 audit 可能继承自 BaseService)。
    # 此时全部节点已归并, 可以按 BELONGS_TO 反查真实方法, 再沿 INHERITS
    # 向上找。拼 uid 猜位置会造出 External 桩 —— 实测 fixture 里
    # AuthService.audit 就是这样被误判成外部符号的。
    methods_of_class = {}
    for rel in relations:
        if rel["rel"] == "BELONGS_TO":
            methods_of_class.setdefault(rel["dst"], []).append(rel["src"])
    # 类 -> 父类 uid (含跨模块, 两种方向都要记)
    parents_of = {}
    for rel in relations:
        if rel["rel"] == "INHERITS":
            parents_of.setdefault(rel["src"], []).append(rel["dst"])

    def find_method(class_uid, attr, depth=0):
        if depth > 10:
            return None
        for m in methods_of_class.get(class_uid, []):
            if m.rsplit(":", 1)[-1].rsplit(".", 1)[-1] == attr:
                return m
        for parent in parents_of.get(class_uid, []):
            hit = find_method(parent, attr, depth + 1)
            if hit:
                return hit
        return None

    resolved_names = set()
    for analyzer in analyzers:
        for caller, var_type, attr in getattr(analyzer, "_pending_var_calls", []):
            hit = find_method(var_type, attr)
            if hit:
                add_relation(caller, "CALLS", hit)
                # 这条调用已在第二遍解析成功, 必须撤销第一遍记的 unresolved,
                # 否则同一条调用既连了真实边又留在 unresolved 清单里。
                resolved_names.add((caller, attr))
                continue
            # 类存在但方法找不到 (外部库/动态属性): 建桩而不是丢边
            add_relation(caller, "CALLS", f"{var_type}.{attr}", dst_kind="Unknown")
            if f"{var_type}.{attr}" not in entities:
                add_entity({
                    "type": "External",
                    "name": f"{var_type}.{attr}",
                    "uid": f"{var_type}.{attr}",
                    "module": var_type.split(":", 1)[0],
                    "filepath": None,
                    "lineno": None,
                    "external": True,
                    "kind": "symbol",
                })

    for analyzer in analyzers:
        analyzer.unresolved_calls = [
            u for u in analyzer.unresolved_calls
            if (u["caller"], u["name"]) not in resolved_names
        ]

    # 4. 判定所有跨模块边: 目标存在则连通, 否则建 :External 桩
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

    def drop_modules(self, module_uids):
        """删除指定 Module 及其定义的符号子图, 返回删除的节点数。

        这是增量更新的关键一步: 只靠 MERGE 写入无法表达"源码里这个函数
        已经没了", 重命名会在图里留下幽灵节点和幽灵 CALLS 边
        (实测证据见 docs/specs/SPEC-incremental.md)。

        按 uid 前缀删除而不是按 name: 同名符号跨模块大量存在,
        按 name 删会误删别的模块的符号。
        """
        uids = list(module_uids)
        if not uids:
            return 0

        deleted = 0
        with self.driver.session() as session:
            for uid in uids:
                # uid 形如 "module:qualname"; 属于本模块的符号必然以
                # "module:" 开头, Module 节点自身 uid 就是 "module"。
                prefix = uid + ":"
                result = session.run(
                    """
                    MATCH (n)
                    WHERE n.uid = $uid OR n.uid STARTS WITH $prefix
                    DETACH DELETE n
                    RETURN count(n) AS deleted
                    """,
                    uid=uid,
                    prefix=prefix,
                ).single()
                deleted += result["deleted"] if result else 0
        return deleted

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
