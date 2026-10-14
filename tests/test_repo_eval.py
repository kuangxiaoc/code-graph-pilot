"""真实仓库评测: 守住解析率与"零悬空边"两条硬指标。

用仓库内自带的小样本而不是下载 psf/requests —— AGENTS.md 要求所有测试
离线且可重复, 依赖外部仓库会让 CI 在网络波动时红。
真实仓库的数字 (requests: 解析率 59.6%, 悬空边 0) 记在 README 里,
由 `benchmarks/repo_eval.py` 单独跑。

这里覆盖的是 repo_eval 的**分类逻辑**: unresolved 必须能区分
内置方法 / 外部 / 真漏检, 否则残差不可读。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from repo_eval import evaluate  # noqa: E402


def test_evaluate_reports_zero_dangling_edges(tmp_path):
    """真实仓库上悬空边必须为 0 —— 这是图完整性的底线。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text(
        "class Response:\n    def json(self):\n        pass\n", encoding="utf-8")
    (pkg / "client.py").write_text(
        "from .models import Response\n\n"
        "def fetch(r: Response):\n    return r.json()\n",
        encoding="utf-8")

    result = evaluate(str(pkg), package_root=str(tmp_path))
    assert result["dangling_edges"] == 0
    # 参数标注 + 相对导入生效: 这条边必须解析到真实定义
    assert result["calls_resolved"] >= 1


def test_unresolved_is_split_into_builtin_and_real_miss(tmp_path):
    """dict.get 不得被算成真漏检 —— 那会让残差不可读。"""
    pkg = tmp_path / "pkg2"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(
        "def get():  # 与 dict.get 同名, 用于验证分类不是按名字\n    pass\n\n"
        "def use(d):\n    return d.get('k')\n",
        encoding="utf-8")

    result = evaluate(str(pkg), package_root=str(tmp_path))
    # d.get 是内置容器方法, 虽然仓库里确实定义了 get(), 仍应归为内置
    assert result["unresolved_builtin"] >= 1
    assert result["unresolved_missed"] == 0


def test_evaluate_json_shape(tmp_path):
    """--json 输出的字段必须稳定, 便于外部消费。"""
    pkg = tmp_path / "pkg3"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    result = evaluate(str(pkg), package_root=str(tmp_path))
    for key in ("files", "symbols", "calls_total", "calls_resolved",
                "resolution_rate", "dangling_edges", "unresolved_total",
                "unresolved_builtin", "unresolved_external",
                "unresolved_missed"):
        assert key in result, key
