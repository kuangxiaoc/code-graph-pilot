"""指标归一化测试: 守护"不把模块名/普通英文词算成幻觉"这条口径。

起因是一次真实 LLM 回答拿到 81.8% 幻觉率, 其中 9 个"幻觉符号"里:
  - auth.middleware / auth.tokens / utils.validators 是模块名
  - High / Python / TypeError / ValueError 是普通英文词
  - 真正的幻觉是 0 个

没有这些归一化, 指标会持续虚高, 最终没人敢看这个数字。
"""

from observability.trace import (
    compute_metrics,
    extract_symbols,
    normalize_symbol,
    normalize_symbols,
)


# --- 归一化 ---

def test_strips_module_prefix_from_uid():
    assert normalize_symbol("utils.validators:validate_token") == "validate_token"


def test_keeps_class_qualifier():
    """Class.method 不能拍平: AuthService.login 与 BaseService.login 是不同符号。"""
    assert normalize_symbol("auth.service:AuthService.login") == "AuthService.login"


def test_plain_identifier_unchanged():
    assert normalize_symbol("validate_token") == "validate_token"


def test_empty_and_none_dropped():
    assert normalize_symbols([None, "", "  "]) == set()


# --- 端到端: 真实回答形态 ---

def test_module_names_are_not_hallucinations():
    """回答里写 'auth.tokens 模块' 不得被判成幻觉。"""
    retrieved = {"auth.tokens:refresh_token", "auth.middleware:authenticate"}
    # 直接传原始文本 (真实管线的调用形态): compute_metrics 内部用 retrieved
    # 作为 known, 从而认出 authenticate 这个全小写符号名。
    response = (
        "需要同步检查 `refresh_token` 与 `authenticate` 两个函数, "
        "它们分别在 auth.tokens 与 auth.middleware 模块中。"
    )
    metrics = compute_metrics(retrieved, response)
    assert metrics["hallucinated"] == []
    assert metrics["hallucination_rate"] == 0.0
    assert metrics["grounding_recall"] == 1.0


def test_common_english_words_are_not_claimed_symbols():
    """'High 风险' / 'TypeError' / 'Python 架构师' 不是符号。"""
    text = "风险等级为 High。可能抛出 TypeError 或 ValueError。作为 Python 架构师建议。"
    claimed = extract_symbols(text)
    assert "High" not in claimed
    assert "TypeError" not in claimed
    assert "ValueError" not in claimed
    assert "Python" not in claimed


def test_module_name_form_is_not_extracted():
    """点分全小写形态 (auth.service) 是模块名, 不应进 claimed。"""
    text = "需要同步修改 auth.middleware 与 auth.tokens 两个模块"
    claimed = extract_symbols(text)
    assert "auth.middleware" not in claimed
    assert "auth.tokens" not in claimed


def test_real_symbols_survive_filtering():
    """真实符号必须仍然抽得到 —— 过滤不能把指标刷成 0。"""
    text = "修改 `validate_token` 会影响 `AuthService.login` 与 `refresh_token`。"
    claimed = extract_symbols(text)
    assert {"validate_token", "AuthService.login", "refresh_token"} <= claimed


def test_grounded_real_answer_scores_zero_hallucination():
    """复现实测那一幕:  grounded 回答修正后应为 0 幻觉、100% 覆盖。"""
    retrieved = {
        "utils.validators:validate_token",
        "auth.tokens:refresh_token",
        "auth.middleware:authenticate",
    }
    response = (
        "修改 `validate_token` 将直接影响 `refresh_token` 与 `authenticate`。"
        "这两个调用者位于 `auth.tokens` 与 `auth.middleware` 模块, 风险等级 High。"
    )
    metrics = compute_metrics(retrieved, response)
    assert metrics["hallucinated"] == []
    assert metrics["hallucination_rate"] == 0.0
    assert metrics["grounding_recall"] == 1.0


def test_genuine_hallucination_still_detected():
    """归一化不得把真实幻觉洗掉 —— 这是指标的立身之本。"""
    retrieved = {"utils.validators:validate_token"}
    response = "`validate_token` 会调用 `totally_fake_dependency`。"
    metrics = compute_metrics(retrieved, response)
    assert "totally_fake_dependency" in metrics["hallucinated"]
    assert metrics["hallucination_rate"] > 0


def test_retrieved_set_is_canonical_only(monkeypatch):
    """retrieved 不得含拼写变体 —— 变体进分母会让覆盖率虚低。"""
    from agent import _retrieved_symbols

    record = {
        "uid": "auth.middleware:authenticate",
        "name": "authenticate",
        "module": "auth.middleware",
        "calls": ["validate_token"],
        "called_by": ["login_route"],
    }
    symbols = _retrieved_symbols(record)
    # 只保留 uid 规范形, 不展开成 authenticate / middleware:authenticate 等
    assert symbols == {"auth.middleware:authenticate", "validate_token", "login_route"}
