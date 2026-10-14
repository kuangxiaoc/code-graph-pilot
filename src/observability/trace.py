"""Agent 轨迹记录与指标计算。

指标定义见 docs/specs/SPEC-observability.md:
    hallucinated       = claimed - retrieved
    hallucination_rate = |hallucinated| / |claimed|
    grounding_recall   = |claimed ∩ retrieved| / |retrieved|

全部为纯集合运算, 不需要 LLM judge —— 判定"图里没有的东西出现在答案里"
是可重复、零成本、无模型偏差的。
"""

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TRACE_PATH = Path("traces/agent.jsonl")

# 密钥形态: 匹配到即从轨迹中抹除, 防止 telemetry 成为泄密路径
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|password)\s*[:=]\s*\S+"),
]

# 中文回答里几乎不可能作为符号出现的常见词, 避免污染 claimed 集合
_STOPWORDS = {
    "None", "True", "False", "self", "NoneType", "print", "len", "str",
    "int", "list", "dict", "set", "bool", "return", "import", "from",
    "TODO", "FIXME", "NOTE",
}

# 标识符: snake_case / CamelCase / 点分限定名 / module:qualname 的 qualname 部分
# 允许前面带 "module:" 前缀 (回答里可能写 auth.service:AuthService.login);
# 抽取后只保留冒号后的 qualname 部分, 与 retrieved 的键对齐。
_IDENT_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?:[A-Za-z_][A-Za-z0-9_]*:)?"       # 可选的 module: 前缀
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*"      # 可选的点分前缀 (auth.service)
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*"      # 可选的点分后缀 (AuthService.login)
    r"(?![A-Za-z0-9_.])"
)


def _redact(text):
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _looks_like_identifier(token):
    """过滤掉普通英文单词: 只保留 snake_case / CamelCase / 含点的限定名。"""
    if token in _STOPWORDS:
        return False
    if "." in token:
        return True
    if "_" in token:
        return True
    # CamelCase: 至少一个大写字母开头且内含小写, 或含连续大写
    if token[0].isupper() and any(c.islower() for c in token):
        return True
    if any(c.isupper() for c in token[1:]):
        return True
    return False


def extract_symbols(text):
    """从回答文本中抽取符号名。

    反引号片段最可信, 但为保证召回, 对全文同样抽取。
    注意: 中文回答里用"这个函数"指代而未写出符号名的部分无法抽取,
    因此该函数的结果是**下界** (见 SPEC-observability.md Open Questions)。
    """
    if not text:
        return set()
    found = set()
    for match in _IDENT_RE.finditer(text):
        token = match.group(0)
        # module:qualname 形式只取 qualname 部分
        if ":" in token:
            token = token.split(":", 1)[1]
        if _looks_like_identifier(token):
            found.add(token)
    return found


def compute_metrics(retrieved, claimed, retrieval_failed=False):
    """计算幻觉率与覆盖率。

    retrieval_failed=True 时 retrieved 为空是检索失败导致的, 不是模型没有
    事实可依。此时报 0.0 会掩盖问题, 报 1.0 会把 "连不上库" 误报成
    "模型在编造", 因此置 None 表示不可判定, 由报告层单独统计。
    """
    retrieved = set(retrieved)
    claimed = set(claimed)
    hallucinated = claimed - retrieved
    grounded = claimed & retrieved
    missed = retrieved - claimed

    if retrieval_failed:
        hallucination_rate = None
        grounding_recall = None
    else:
        hallucination_rate = len(hallucinated) / len(claimed) if claimed else 0.0
        grounding_recall = len(grounded) / len(retrieved) if retrieved else 0.0

    return {
        "hallucination_rate": hallucination_rate,
        "grounding_recall": grounding_recall,
        "hallucinated": sorted(hallucinated),
        "grounded": sorted(grounded),
        "missed": sorted(missed),
        "claimed_count": len(claimed),
        "retrieved_count": len(retrieved),
        "retrieval_failed": retrieval_failed,
    }


def record_trace(path=None, **fields):
    """把一次 run 写入 JSONL。返回记录 dict (便于测试断言), 禁用时返回 None。"""
    if os.getenv("CODEGRAPH_TRACE", "1") == "0":
        return None

    path = Path(path) if path else DEFAULT_TRACE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    response = fields.get("response") or ""
    retrieved = set(fields.get("retrieved_symbols") or ())
    claimed = extract_symbols(response)
    retrieval_failed = bool(fields.get("retrieval_failed"))

    record = {
        "run_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "query": _redact(str(fields.get("query", ""))),
        "target_func": fields.get("target_func"),
        "nodes_visited": fields.get("nodes_visited", []),
        "revision_count": fields.get("revision_count", 0),
        "review_verdicts": fields.get("review_verdicts", []),
        "terminated_by": fields.get("terminated_by", "unknown"),
        "duration_ms": fields.get("duration_ms"),
        "retrieved_symbols": sorted(retrieved),
        "claimed_symbols": sorted(claimed),
        "retrieval_failed": retrieval_failed,
        "metrics": compute_metrics(retrieved, claimed, retrieval_failed),
    }

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return record


class TraceTimer:
    """测量一段耗时 (毫秒)。"""

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = int((time.perf_counter() - self._start) * 1000)
        return False
