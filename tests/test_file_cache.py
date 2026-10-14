"""文件 hash 缓存测试: 复用上次解析结果, 只重解析变更文件。

关键正确性约束: 缓存路径产出的图必须与全量解析**完全一致**。
跨文件解析 (resolve_graph) 需要所有文件节点可见, 因此缓存的是
Analyzer 的产出而不是"跳过文件" —— 测这一点不能只看耗时。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import file_cache  # noqa: E402
from graph_builder import CodeAnalyzer, resolve_graph  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """两个文件: a 调用 b 里的函数, 保证跨文件解析确实发生。"""
    (tmp_path / "a.py").write_text(
        "from b import helper\n\ndef use():\n    return helper()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _files(root):
    return sorted(str(p) for p in Path(root).glob("*.py"))


def _full(root):
    ans = [CodeAnalyzer(f, module=Path(f).stem) for f in _files(root)]
    return resolve_graph(ans)


def test_partition_detects_unchanged(repo):
    files = _files(repo)
    # 首次: 全部需解析
    todo, reuse = file_cache.partition(str(repo), files)
    assert len(todo) == 2 and reuse == []

    ans = [CodeAnalyzer(f, module=Path(f).stem) for f in files]
    assert file_cache.save(str(repo), ans)

    todo, reuse = file_cache.partition(str(repo), files)
    assert todo == [] and len(reuse) == 2


def test_partition_detects_change(repo):
    files = _files(repo)
    ans = [CodeAnalyzer(f, module=Path(f).stem) for f in files]
    file_cache.save(str(repo), ans)

    (repo / "a.py").write_text(
        "from b import helper\n\ndef use():\n    return helper() + 1\n",
        encoding="utf-8")
    todo, reuse = file_cache.partition(str(repo), files)
    assert todo == [str(repo / "a.py")]
    assert len(reuse) == 1


def test_cached_graph_matches_full_parse(repo):
    """缓存路径必须与全量解析产出同一张图 —— 这是本层的核心不变量。"""
    files = _files(repo)
    full_ents, full_rels = _full(repo)

    ans = [CodeAnalyzer(f, module=Path(f).stem) for f in files]
    file_cache.save(str(repo), ans)
    cached = file_cache.load_cached(str(repo), files)
    assert cached is not None and len(cached) == 2

    ents, rels = resolve_graph(cached)
    assert {e["uid"] for e in ents} == {e["uid"] for e in full_ents}
    assert {(r["src"], r["rel"], r["dst"]) for r in rels} == \
           {(r["src"], r["rel"], r["dst"]) for r in full_rels}


def test_load_cached_returns_none_when_incomplete(repo):
    """缺任一个文件就整体退回全量 —— 半张缓存比没有更危险。"""
    files = _files(repo)
    ans = [CodeAnalyzer(f, module=Path(f).stem) for f in files]
    file_cache.save(str(repo), ans)
    assert file_cache.load_cached(str(repo), files) is not None
    # 多要一个没缓存过的文件
    assert file_cache.load_cached(str(repo), files + ["/nope.py"]) is None


def test_save_merges_previous_index(repo):
    """只保存变更文件时, 未变更文件的索引条目不得丢失。

    丢了的话下一次构建会全量重解析, 缓存等于没生效。
    """
    files = _files(repo)
    ans = [CodeAnalyzer(f, module=Path(f).stem) for f in files]
    file_cache.save(str(repo), ans)
    index = file_cache.load_index(str(repo))

    # 只保存其中一个文件, 带上 previous_index
    (repo / "a.py").write_text("def use():\n    return 2\n", encoding="utf-8")
    fresh = [CodeAnalyzer(str(repo / "a.py"), module="a")]
    file_cache.save(str(repo), fresh, previous_index=index)

    merged = file_cache.load_index(str(repo))
    assert len(merged) == 2, "未变更文件的索引条目丢了"


def test_corrupt_index_falls_back_to_empty(repo):
    """索引损坏时退回全量解析, 不得让构建失败。"""
    idx = file_cache._index_path(str(repo))
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text("{not json", encoding="utf-8")
    assert file_cache.load_index(str(repo)) == {}


def test_parser_version_invalidates_cache(repo):
    """解析器版本变了必须整体作废, 不做细粒度迁移。"""
    files = _files(repo)
    ans = [CodeAnalyzer(f, module=Path(f).stem) for f in files]
    file_cache.save(str(repo), ans)
    assert file_cache.load_cached(str(repo), files) is not None

    orig = file_cache.PARSER_VERSION
    try:
        file_cache.PARSER_VERSION = orig + 1
        assert file_cache.load_cached(str(repo), files) is None
    finally:
        file_cache.PARSER_VERSION = orig
