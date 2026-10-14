"""文件 hash 缓存: 增量构建时只重解析变更的文件。

为什么要有这一层
----------------
原来的 --keep 增量仍需**全量解析**整个目录, 只省了写库。跨文件解析
(resolve_graph) 需要所有文件的节点可见才能判定调用目标, 因此不能简单
跳过未变更文件 —— 必须缓存它们的解析结果。

缓存什么
--------
缓存的是 CodeAnalyzer 的**产出** (entities / relations / external_relations
/ 待解析调用), 不是源文件。重放时构造一个轻量 Analyzer 替身参与
resolve_graph, 从而未变更文件的节点照旧可见, 但不重跑 ast.parse。

边界 (诚实说明)
--------------
- 收益只在"文件多、改动少"且解析是瓶颈时显现。实测 300 文件全量解析
  约 25ms, hash 300 文件约 4ms —— 小仓库上本层是净亏损, 因此
  **默认不启用**, 需显式 --cache。
- 解析器的行为变更会让缓存失效, 故缓存带上解析器版本号; 版本变了
  整体丢弃重算, 不试图做细粒度迁移。
"""

import hashlib
import json
import pickle  # noqa: S403 - 只反序列化本进程自己写的文件
from pathlib import Path

# 解析结果格式或解析逻辑变更时递增。旧缓存会被整体丢弃。
PARSER_VERSION = 1

_DEFAULT_CACHE_DIR = ".codegraph_cache"


def file_hash(path):
    """文件内容的 sha256。读失败返回 None (调用方视为需重解析)。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _cache_path(root):
    return Path(root) / _DEFAULT_CACHE_DIR


def _index_path(root):
    return _cache_path(root) / ("index-v%d.json" % PARSER_VERSION)


def _blob_path(root, key):
    return _cache_path(root) / ("v%d-%s.pkl" % (PARSER_VERSION, key))


def load_index(root):
    """读取 {文件路径: hash} 索引; 不存在或损坏时返回空 dict。"""
    p = _index_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # 缓存损坏不该让构建失败 —— 退回全量解析即可
        return {}


def partition(root, files):
    """把文件分成 (需重解析, 可复用)。"""
    old = load_index(root)
    todo, reuse = [], []
    for f in files:
        h = file_hash(f)
        if h is not None and old.get(f) == h:
            reuse.append(f)
        else:
            todo.append(f)
    return todo, reuse


def save(root, analyzers, previous_index=None):
    """把解析结果写入缓存, 并与上次索引合并。

    previous_index 必须合入: 本次调用只传入**变更过的**文件, 若直接用
    它们的 hash 覆盖整个索引, 未变更文件的条目会全部丢失, 下一次构建
    就会全量重解析 —— 缓存等于没生效。
    """
    cache = _cache_path(root)
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    index = dict(previous_index or {})
    ok = True
    for a in analyzers:
        h = file_hash(a.filepath)
        if h is None:
            continue
        index[a.filepath] = h
        blob = {
            "filepath": a.filepath,
            "module": a.module,
            "entities": a.entities,
            "relations": a.relations,
            "external_relations": a.external_relations,
            "unresolved_calls": a.unresolved_calls,
            "_pending_var_calls": getattr(a, "_pending_var_calls", []),
        }
        try:
            _blob_path(root, h).write_bytes(pickle.dumps(blob, protocol=4))
        except OSError:
            ok = False

    try:
        _index_path(root).write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        ok = False
    return ok


class CachedAnalyzer:
    """缓存重放出的 Analyzer 替身。

    只提供 resolve_graph 真正用到的字段, 不重新解析源码。
    """

    def __init__(self, blob):
        self.filepath = blob["filepath"]
        self.module = blob["module"]
        self.source = None
        self.entities = blob["entities"]
        self.relations = blob["relations"]
        self.external_relations = blob["external_relations"]
        self.unresolved_calls = blob["unresolved_calls"]
        self._pending_var_calls = blob.get("_pending_var_calls", [])


def load_cached(root, files):
    """按文件列表重放缓存; 缺任一文件就返回 None (调用方退回全量)。"""
    index = load_index(root)
    out = []
    for f in files:
        h = index.get(f)
        if h is None:
            return None
        p = _blob_path(root, h)
        if not p.exists():
            return None
        try:
            out.append(CachedAnalyzer(pickle.loads(p.read_bytes())))  # noqa: S301
        except Exception:  # noqa: BLE001 - 缓存损坏退回全量, 不中断构建
            return None
    return out
