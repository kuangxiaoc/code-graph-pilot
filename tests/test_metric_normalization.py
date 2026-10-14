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


# --- Python 列表字面量 (prompt 要求模型原样引用图谱输出, 于是它回贴列表) ---

def test_list_literal_is_split_not_treated_as_one_symbol():
    """`['a:b', 'c:d']` 必须拆开逐项抽取, 否则正则产出碎片。"""
    text = "图谱记录: `['auth.tokens:refresh_token', 'auth.middleware:authenticate']`"
    claimed = extract_symbols(text)
    assert "refresh_token" in claimed
    assert "authenticate" in claimed
    # 不得出现碎片: 半个引号 / 逗号拼接 / 括号
    assert not any("]" in c or "'" in c or "[" in c for c in claimed)


def test_placeholder_in_list_literal_is_not_a_symbol():
    """"无 (None)" 是 prompt 模板原文, 不是符号。"""
    assert extract_symbols("图谱记录: `['无 (None)']`") == set()


def test_mixed_quote_and_prose():
    """真实回答形态: 反引号符号 + 列表字面量 + 小标题混排。"""
    text = (
        "### 🔽 下游依赖影响 (Outbound)\n"
        "- **图谱记录**: `['无 (None)']`\n"
        "- 修改 `validate_token` 将影响 `['refresh_token', 'authenticate']`\n"
        "- 参见 Inbound Callers 一节"
    )
    claimed = extract_symbols(text)
    assert "validate_token" in claimed
    assert "refresh_token" in claimed
    assert "authenticate" in claimed
    # 小标题与占位符都不得进来
    assert "Callers" not in claimed
    assert not any("None" in c or "无" in c for c in claimed)


def test_grounding_with_list_literal_quotes():
    """端到端: 模型用列表字面量复述检索结果时, 覆盖率必须是 100%。"""
    retrieved = {
        "utils.validators:validate_token",
        "auth.tokens:refresh_token",
        "auth.middleware:authenticate",
    }
    response = (
        "修改 `validate_token` 的上游为 "
        "`['auth.tokens:refresh_token', 'auth.middleware:authenticate']`, "
        "下游为 `['无 (None)']`。风险等级高。"
    )
    metrics = compute_metrics(retrieved, response)
    assert metrics["grounding_recall"] == 1.0
    assert metrics["hallucinated"] == []


# --- 建议语境 / 路径 / 装饰器 (30 次真实 LLM 基线暴露, 详见下) ---
#
# 起因: 30 次基线跑出 40.4% 幻觉率, 逐条人工审计 105 个 flagged 符号后
# 发现**真幻觉为 0**, 全部是抽取假阳性。修完降到 3.6%。这些测试锁住
# 每类假阳性, 防止以后有人改抽取规则时把它们放回来。

def test_proposed_new_name_is_not_hallucination():
    """模型故意编的新名字 ("建议新增 `validate_token_v2`") 不算幻觉。"""
    for name in ("validate_token_v2", "authenticate_v2", "login_v2",
                 "validate_token_detailed", "legacy_wrapper", "new_adapter"):
        assert extract_symbols("建议新增 `%s` 做渐进式迁移" % name) == set(), name


def test_file_path_segments_are_not_symbols():
    """路径里的目录名 (webapp_py) 不得被抽成符号 —— 实测 14/14 次都源于此。"""
    text = ("**文件位置** | `benchmarks/fixtures/webapp_py/utils/validators.py`"
            " (第 8 行) 修改 `validate_token`")
    claimed = extract_symbols(text)
    assert "validate_token" in claimed
    assert "webapp_py" not in claimed
    assert not any("fixtures" in c or "benchmarks" in c for c in claimed)


def test_decorator_and_typing_are_not_symbols():
    assert extract_symbols("保留旧方法签名为废弃别名 (`@deprecated`)") == set()
    assert extract_symbols("考虑返回兼容对象 (`NamedTuple`)") == set()


def test_python_keywords_and_stdlib_are_not_symbols():
    for kw in ("async", "await", "tuple", "datetime", "secrets", "hashlib"):
        assert extract_symbols("若改为 `%s` 需注意上游" % kw) == set(), kw


def test_bare_method_name_matches_qualified_uid():
    """图里存 AuthService.login, 回答写裸名 login —— 必须能对齐。

    注意 recall 只算 0.5: wrapper 确实没被提到, grounded 只含 AuthService.login。
    这里断言的是"裸名不被判成幻觉", 而不是"recall 必须满分"。
    """
    retrieved = {"auth.service:AuthService.login", "auth.middleware:wrapper"}
    response = "若修改了 `login` 的参数签名, 4 个调用方都会受影响。"
    metrics = compute_metrics(retrieved, response)
    assert metrics["hallucinated"] == []
    assert metrics["grounded"] == ["AuthService.login"]


def test_recall_denominator_is_not_inflated_by_bare_names():
    """裸名只能用于匹配, 不得塞进 retrieved —— 否则 recall 系统性虚低。"""
    retrieved = {"auth.service:AuthService.login"}
    # 只提到裸名 login, 没提 wrapper 等其他符号
    metrics = compute_metrics(retrieved, "修改 `login` 影响面很大。")
    assert metrics["grounding_recall"] == 1.0
    assert metrics["retrieved_count"] == 1


def test_module_name_is_not_expanded_into_symbol():
    """utils.validators 不得展开出 validators —— 那会凭空造符号。"""
    retrieved = {"utils.validators:validate_token"}
    response = "该符号位于 `utils.validators` 与 `validate_token`。"
    metrics = compute_metrics(retrieved, response)
    assert "validators" not in metrics["hallucinated"]


def test_full_real_response_scores_low():
    """端到端: 一段真实的重构建议回答, 幻觉率必须为 0。"""
    retrieved = {
        "auth.middleware:authenticate",
        "auth.tokens:refresh_token",
        "utils.validators:validate_token",
    }
    response = """
    # `validate_token` 修改影响分析报告
    **文件位置** | `benchmarks/fixtures/webapp_py/utils/validators.py` (第 8 行)
    该函数存在 2 个上游调用者: `refresh_token` 与 `authenticate`。
    1. 保持向后兼容: 新增参数使用默认值; 破坏性变更建议新增
       `validate_token_v2` 而非原地修改。
    2. 返回值可考虑 `NamedTuple`; 旧签名保留为 `@deprecated` 别名。
    3. 若改为 `async` 需注意上游未 `await`。
    """
    metrics = compute_metrics(retrieved, response)
    assert metrics["hallucinated"] == []
    assert metrics["hallucination_rate"] == 0.0
    assert metrics["grounding_recall"] == 1.0
