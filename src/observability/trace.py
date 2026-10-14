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

# 普通英文词与技术通用词。它们会出现在中文技术回答里 ("High 风险",
# "TypeError", "Python 架构师"), 但不是被检索到的代码符号。
# 不过滤的后果: 幻觉率里混入一批与图无关的词, 使指标不可读
# (实测一次真实回答 81.8% 幻觉率中, 5/11 是这类词)。
_COMMON_WORDS = {
    # 风险等级 / 修饰词
    "High", "Medium", "Low", "high", "medium", "low", "Critical", "None",
    # 语言与框架通用词
    "Python", "Java", "Go", "Rust", "JavaScript", "TypeScript",
    # 异常与内置类型 (回答里解释"会抛什么错"时必然出现)
    "TypeError", "ValueError", "KeyError", "IndexError", "AttributeError",
    "RuntimeError", "Exception", "AssertionError",
    # 回答自身结构与建议用语
    "Outbound", "Inbound", "TODO", "Note", "Warning", "Error",
    "Signature", "Refactor", "Impact", "Context", "Result", "Summary",
    # 重构建议里必然出现的通用技术词。它们与图无关, 计入 claimed 只会
    # 稀释幻觉率的意义 (实测一次回答 3/6 的"幻觉"全是这类词)。
    "Protocol", "ABC", "IDE", "API", "CLI", "SDK", "HTTP", "JSON", "URL",
    "LSP", "AST", "CFG", "PDG", "CKG", "RDF", "OWL", "SPARQL",
}

# 模块名形态: 全小写点分 (auth.service, utils.validators)。
# 这些是模块而不是符号, 但真实回答里常以 "auth.middleware 模块" 的形式出现。
_MODULE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")

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
    if token in _COMMON_WORDS:
        return False
    if "." in token:
        # 模块名 (auth.service) 不是被检索的符号; 保留 Class.method 与
        # 文件路径 (service.py 形态已由正则的下半段排除)。
        if _MODULE_NAME_RE.match(token):
            return False
        return True
    if "_" in token:
        return True
    # 全大写缩写 (CI / ABC / IDE): 几乎全是通用词而非代码符号。
    # 反引号路径已在 extract_symbols 里单独处理, 走到这里说明是裸词。
    if token.isupper():
        return False
    # CamelCase: 至少一个大写字母开头且内含小写, 或含连续大写
    if token[0].isupper() and any(c.islower() for c in token):
        return True
    if any(c.isupper() for c in token[1:]):
        return True
    return False


def extract_symbols(text, known=None):
    """从回答文本中抽取符号名。

    反引号片段最可信, 但为保证召回, 对全文同样抽取。

    抽取规则 (按可信度):
    1. **反引号片段** —— 模型显式标注为代码, 最可信, 接受任意形态
    2. **裸标识符** —— 仅 snake_case (validate_token) 与点分限定名
       (AuthService.login)。**纯首字母大写词不算** (Callers / Dependencies):
       它们大量来自回答自身的小标题 ("Inbound Callers") 与建议用语,
       手工 blocklist 追不上 (实测三轮各自冒出 ABC/Protocol、
       CI/Callers/Dependencies)。要认它们, 模型必须打反引号。
    3. known 里的全小写符号名 —— 过不了形态过滤但图里真实存在

    这个不对称是刻意的: 漏抽只会**低估**幻觉率 (安全方向),
    而误抽会让指标失去意义。

    known: 本次 run 实际检索到的符号集合。传入后, 其中全小写的名字
    (authenticate / login / save) 也会被识别 —— 这类名字过不了
    _looks_like_identifier 的形态过滤, 但它们是图里真实存在的符号。
    不传 known 时这些符号对指标完全不可见 (fixture 里 26 个符号有 4 个
    属于这类, 占 15%), 覆盖率会被结构性低估。

    注意: 中文回答里用"这个函数"指代而未写出符号名的部分无法抽取,
    因此该函数的结果是**下界** (见 SPEC-observability.md Open Questions)。
    """
    if not text:
        return set()

    found = set()

    # 1. 反引号片段: 最可信, 不做形态过滤 (但排除模块名与常见词)
    for match in re.finditer(r"`([^`]+)`", text):
        token = match.group(1).strip()
        if ":" in token:
            token = token.split(":", 1)[1]
        if token and token not in _STOPWORDS and token not in _COMMON_WORDS:
            if not _MODULE_NAME_RE.match(token):
                found.add(token)

    # 检索到的符号按规范形建索引, 用于兜底识别全小写名字
    known_norm = normalize_symbols(known)

    for match in _IDENT_RE.finditer(text):
        token = match.group(0)
        # module:qualname 形式只取 qualname 部分
        if ":" in token:
            token = token.split(":", 1)[1]
        if _is_bare_symbol(token) or token in known_norm:
            found.add(token)
    return found


def _is_bare_symbol(token):
    """裸标识符是否算符号: 只要 snake_case 与点分限定名。

    刻意排除首字母大写的单个词 (Callers / Dependencies / Protocol):
    它们在中文技术回答里几乎总是小标题或建议用语, 不是代码符号。
    模型若真是指代码, 会打反引号 —— 那条路径不受此限。
    代价是 Class 名 (AuthService) 单独出现时会被漏抽, 这是可接受的
    低估方向 (漏抽低估幻觉率, 误抽则让指标失去意义)。
    """
    if token in _STOPWORDS or token in _COMMON_WORDS:
        return False
    if "." in token:
        return not _MODULE_NAME_RE.match(token)
    if "_" in token:
        return True
    return False


def normalize_symbol(name):
    """把符号统一成可比较的规范形。

    同一个符号在回答里有三种常见写法, 不归一就会误判成幻觉:
        utils.validators:validate_token   ->  validate_token
        auth.service:AuthService.login    ->  AuthService.login
        utils.validators                  ->  utils.validators (模块名, 原样保留)

    规则:
    - 去掉 "module:" 前缀 (uid 的命名空间部分)
    - 保留点分层级: Class.method 与方法名 login 是**不同符号**, 不能拍平,
      否则 AuthService.login 与 BaseService.login 会被当成同一个
    - 纯标识符原样返回

    为什么不在 retrieved 侧做变体展开 (曾经的实现): 那会把
    "middleware:authenticate" 这类中间形态塞进分母, 使覆盖率虚低。
    归一化两侧是对称的, 不改变任何一侧的语义。
    """
    if not name:
        return ""
    name = name.strip()
    # uid 形如 "module:qualname"; 取冒号后的部分
    if ":" in name:
        name = name.split(":", 1)[1]
    return name


def normalize_symbols(names):
    """集合版归一化; 空串与 None 被丢弃。"""
    out = set()
    for n in names or ():
        norm = normalize_symbol(n)
        if norm:
            out.add(norm)
    return out


def compute_metrics(retrieved, claimed, retrieval_failed=False, known=None):
    """计算幻觉率与覆盖率。

    retrieval_failed=True 时 retrieved 为空是检索失败导致的, 不是模型没有
    事实可依。此时报 0.0 会掩盖问题, 报 1.0 会把 "连不上库" 误报成
    "模型在编造", 因此置 None 表示不可判定, 由报告层单独统计。

    两侧集合先经 normalize_symbol() 归一化再比较: 同一个符号在回答里可能
    写成裸名 (validate_token)、模块限定名 (utils.validators:validate_token)
    或模块名本身 (utils.validators)。不归一会把这些全部判成幻觉 —— 实测
    真实 LLM 的一次回答因此拿到 81.8% 的幻觉率, 而其中 9 个"幻觉"里
    5 个是模块名/普通英文词的归一化问题, 不是模型编造。

    known: 若 claimed 是**原始回答文本**而非已抽取的集合, 传 retrieved 作为
    known, 使全小写符号名 (authenticate) 也能被识别。
    """
    if isinstance(claimed, str):
        claimed = extract_symbols(claimed, known=retrieved)
    retrieved = normalize_symbols(retrieved)
    claimed = normalize_symbols(claimed)
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
    # 把 retrieved 传给抽取器: 全小写符号名 (authenticate) 过不了形态过滤,
    # 但它们是图里真实存在的符号, 必须能被识别。
    claimed = extract_symbols(response, known=retrieved)
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
        # 保留脱敏后的回答, 使"为什么这个符号算幻觉"可复盘。
        # 缺了它, 看到幻觉率只能猜, 无法区分"模型编造"与"抽取规则漏了"。
        # 实测价值: 上一轮就是靠它定位到 ABC/Protocol 是建议用语而非幻觉。
        "response": _redact(response),
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
