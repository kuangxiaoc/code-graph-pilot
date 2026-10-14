"""Agent 模块测试: 不调 LLM。DB 相关用例显式声明用 live_db / dead_db fixture。"""

import os
from pathlib import Path

import pytest

import agent
from agent import extract_target_symbol

def test_agent_module_imports_without_api_key():
    """agent.py 必须能在无 QWEN_API_KEY 时被导入, 否则图逻辑无法离线测试。"""
    assert agent.app_graph is not None
    assert callable(agent.query_dependencies)

def test_workflow_has_expected_nodes():
    nodes = set(agent.app_graph.get_graph().nodes)
    assert {"parse", "retrieve", "generate", "review"} <= nodes

def test_query_dependencies_reports_connection_error_gracefully(monkeypatch):
    """数据库不可达时应返回可读错误字符串, 而不是抛异常打断工作流。"""
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("NEO4J_USER", "x")
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    result = agent.query_dependencies("anything")
    # 契约: 失败时返回 {'error': ...} 且无 uid。
    # 有 uid 会被判成"检索成功", 空 retrieved 就会把幻觉率算成 1.0。
    assert "uid" not in result
    assert "错误" in result["error"]
    # 渲染层必须能把 error 原样带给 LLM
    assert "错误" in agent.render_context(result)


def test_query_dependencies_result_is_renderable(dead_db, monkeypatch):
    """结构化记录必须能被 render_context 无损渲染 —— 否则 LLM 收不到上下文。"""
    record = {
        "uid": "auth.service:AuthService.login",
        "name": "login",
        "module": "auth.service",
        "filepath": "/x/auth/service.py",
        "lineno": 12,
        "calls": ["decode_token"],
        "called_by": [],
    }
    text = agent.render_context(record)
    assert "auth.service:AuthService.login" in text
    assert "decode_token" in text
    # 空上游必须显式说明, 否则 LLM 会把"没有数据"当成"没有调用者"
    assert "无" in text


def test_retrieved_symbols_filters_none_entries():
    """Cypher collect() 无匹配时返回 [None], 不得进入 retrieved 集合。"""
    from agent import _retrieved_symbols

    record = {"uid": "a:b", "calls": [None], "called_by": [None]}
    assert _retrieved_symbols(record) == {"a:b"}


def test_retrieved_symbols_includes_target_itself():
    """目标自身必须计入 retrieved: 回答里必然复述目标名。"""
    from agent import _retrieved_symbols

    record = {"uid": "m:validate_token", "name": "validate_token",
              "calls": [], "called_by": ["authenticate"]}
    symbols = _retrieved_symbols(record)
    # 只存规范形 (uid), 不展开拼写变体 —— 变体进分母会让覆盖率虚低
    assert symbols == {"m:validate_token", "authenticate"}


def test_retrieved_symbols_are_canonical_uids_only():
    """retrieved 只存规范形 (uid), 不展开拼写变体。

    变体展开是曾经的实现, 后果是分母里塞进 "middleware:authenticate"
    这类中间形态, 覆盖率被虚低 (实测 100% 被算成 22%)。
    多种写法的对齐由 compute_metrics 的归一化负责, 两侧对称。
    """
    from agent import _retrieved_symbols

    record = {"uid": "auth.service:AuthService.login", "name": "login",
              "calls": ["decode_token"], "called_by": []}
    assert _retrieved_symbols(record) == {
        "auth.service:AuthService.login", "decode_token"
    }


def test_retrieved_symbols_drops_module_and_filepath():
    """module 名与 filepath 不是符号, 不得进 retrieved。"""
    from agent import _retrieved_symbols

    record = {"uid": "auth.service:AuthService.login", "name": "login",
              "module": "auth.service", "filepath": "/x/auth/service.py",
              "calls": ["decode_token"], "called_by": []}
    symbols = _retrieved_symbols(record)
    # module 名与 filepath 从未进入 retrieved: 只从结构化字段取符号,
    # 不从渲染文本正则抽取 (旧实现的坑)。
    assert "auth.service" not in symbols
    assert "service.py" not in symbols
    assert symbols == {"auth.service:AuthService.login", "decode_token"}


def test_retrieval_failed_detects_missing_uid():
    from agent import _retrieval_failed

    assert _retrieval_failed({"error": "未找到"}) is True
    assert _retrieval_failed({"uid": "a:b", "calls": []}) is False
    assert _retrieval_failed({}) is True


class _FakeLLM:
    """按调用顺序返回可控回答, 用于离线验证工作流与轨迹。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def invoke(self, prompt):
        # 超出预定序列时重复最后一个: 让"审查持续 FAIL"的用例可写
        idx = min(self.calls, len(self.responses) - 1)
        text = self.responses[idx]
        self.calls += 1
        return type("R", (), {"content": text})()


@pytest.fixture
def live_db(monkeypatch):
    """假 store 模拟"图检索成功", 不连 Neo4j。

    AGENTS.md 要求所有测试离线 (不连 Neo4j)。此前这里真的连本地库并灌
    fixture 图, 代价是整套测试从约 3 秒涨到 149 秒 —— 连不上时 neo4j 驱动
    的指数退避重试是主因。改成假 store 后同样覆盖全部工作流分支。

    名字保留 live_db: 它表达的是"检索成功"这个语义, 与 dead_db 相对。
    """
    import graph_store as gs

    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "password123")
    gs.reset_store()

    store = _FakeStore({
        "uid": "utils.validators:validate_token",
        "name": "validate_token",
        "module": "utils.validators",
        "filepath": "/x/utils/validators.py",
        "lineno": 8,
        "labels": ["Function"],
        "calls": [],
        "called_by": ["auth.tokens:refresh_token", "auth.middleware:authenticate"],
    }, {
        # Method 级节点: 用于验证点分限定名解析到 Method 而非 Class
        "uid": "auth.service:AuthService.login",
        "name": "login",
        "module": "auth.service",
        "filepath": "/x/auth/service.py",
        "lineno": 20,
        "labels": ["Method"],
        "calls": [],
        "called_by": ["routes.api:login_route"],
    })
    monkeypatch.setattr(gs, "get_store", lambda: store)
    return store


class _FakeStore:
    """最小假 store: 只有 symbol() 会被 agent 的检索节点调用。"""

    def __init__(self, *records):
        # 两种形态都要支持: 正常符号记录 (含 uid), 以及固定错误响应
        # ({"error": ...}) —— dead_db 等用例用后者模拟连接失败。
        self.records = {r["uid"]: r for r in records if "uid" in r}
        self.errors = [r for r in records if "uid" not in r]
        self.calls = []

    def symbol(self, name):
        self.calls.append(name)
        if self.errors:
            return dict(self.errors[0])
        # 只认已知符号 (含点分限定名): 其他名字报"未找到", 这样
        # "跳过不在图中的候选"这个行为才可测 (对任何名字都返回成功的话
        # 测不出跳过逻辑)。
        # 只按 uid 的限定名部分精确命中:
        #   validate_token      -> utils.validators:validate_token
        #   AuthService.login   -> auth.service:AuthService.login
        # 刻意不按裸名 (rec["name"]) 匹配 —— Method 的裸名是 login,
        # 会让裸名 login 命中 Method, 使"图中没有 login 时必须跳过候选"
        # 这个用例失效。
        for uid, rec in self.records.items():
            qualname = uid.split(":", 1)[1] if ":" in uid else uid
            if name == qualname:
                return dict(rec)
        return {"error": f"⚠️ 在知识图谱中未找到符号 '{name}'。"}

    def close(self):
        pass


@pytest.fixture
def dead_db(monkeypatch):
    """假 store 模拟"检索失败": 必须与 live_db 严格区分。"""
    import graph_store as gs

    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("NEO4J_USER", "x")
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    gs.reset_store()

    store = _FakeStore({"error": "数据库连接错误: 连接被拒绝"})
    monkeypatch.setattr(gs, "get_store", lambda: store)
    return store


def _install_fake_llm(monkeypatch, responses):
    fake = _FakeLLM(responses)
    # 必须返回同一个实例: agent 的 get_llm() 在每个节点各调一次,
    # 若每次新建则调用计数永远停在 0, "审查持续 FAIL" 的用例无法构造。
    monkeypatch.setattr(agent, "get_llm", lambda: fake)
    return fake


def test_terminated_by_is_pass_when_review_passes(live_db, monkeypatch, tmp_path):
    _install_fake_llm(monkeypatch, ["分析了 `process_data` 的影响", "PASS"])
    result, trace = agent.run_analysis(
        "修改 validate_token 会影响谁", trace_path=tmp_path / "t.jsonl"
    )
    assert result["terminated_by"] == "pass"
    assert trace["revision_count"] == 0
    # 库不可达时为 None (不可判定), 可达时为 0.0~1.0
    rate = trace["metrics"]["hallucination_rate"]
    assert rate is None or 0.0 <= rate <= 1.0


def test_terminated_by_is_max_revision_when_review_keeps_failing(live_db, monkeypatch, tmp_path):
    """审查持续 FAIL 时必须记为 max_revision —— 旧实现在这里无法区分。"""
    _install_fake_llm(monkeypatch, ["第一次回答", "FAIL: 漏上游", "第二次回答", "FAIL"])
    result, trace = agent.run_analysis(
        "修改 validate_token 会影响谁", trace_path=tmp_path / "t.jsonl"
    )
    assert result["terminated_by"] == "max_revision"
    # revision_count 统计的是"审查未通过的次数": 第一次 FAIL 后重试,
    # 第二次 FAIL 次数用尽 -> 共 2 次审查失败。
    assert trace["revision_count"] == 2
    assert len(trace["review_verdicts"]) == 2


def test_nodes_visited_records_full_path(live_db, monkeypatch, tmp_path):
    _install_fake_llm(monkeypatch, ["回答", "PASS"])
    result, _ = agent.run_analysis(
        "修改 validate_token 会影响谁", trace_path=tmp_path / "t.jsonl"
    )
    assert result["nodes_visited"] == ["parse", "retrieve", "generate", "review"]


def test_hallucination_is_detected_in_response(live_db, monkeypatch, tmp_path):
    """回答中出现图里没有的符号时必须被计入幻觉。"""
    _install_fake_llm(monkeypatch, ["`totally_made_up_symbol` 会受影响", "PASS"])
    _, trace = agent.run_analysis(
        "修改 validate_token 会影响谁", trace_path=tmp_path / "t.jsonl"
    )
    assert "totally_made_up_symbol" in trace["metrics"]["hallucinated"]
    assert trace["metrics"]["hallucination_rate"] > 0


def test_grounded_response_has_zero_hallucination(live_db, monkeypatch, tmp_path):
    _install_fake_llm(monkeypatch, ["`validate_token` 与 `refresh_token` 受影响", "PASS"])
    _, trace = agent.run_analysis(
        "修改 validate_token 会影响谁", trace_path=tmp_path / "t.jsonl"
    )
    assert trace["metrics"]["hallucination_rate"] == 0.0


def test_db_unreachable_is_not_reported_as_hallucination(dead_db, monkeypatch, tmp_path):
    """库不可达时指标必须标记 retrieval_failed, 而非报幻觉率 1.0。"""
    _install_fake_llm(monkeypatch, ["`some_symbol` 受影响", "PASS"])
    _, trace = agent.run_analysis(
        "修改 validate_token 会影响谁", trace_path=tmp_path / "t.jsonl"
    )
    assert trace["retrieval_failed"] is True
    assert trace["metrics"]["hallucination_rate"] is None


def test_extract_target_from_chinese_question():
    """硬伤 #1: 取最后一个词会把 "修改 process_data 会影响谁" 解析成 "会影响谁"。"""
    assert extract_target_symbol("修改 process_data 会影响谁") == "process_data"


def test_extract_target_from_english_question():
    assert extract_target_symbol("modify process_data") == "process_data"


def test_extract_target_prefers_backticked():
    assert extract_target_symbol("看看 `validate_token` 改了会怎样") == "validate_token"


def test_extract_target_handles_qualified_name():
    assert extract_target_symbol("AuthService.login 的影响") == "AuthService.login"


def test_extract_target_returns_empty_when_no_identifier():
    assert extract_target_symbol("这个函数的影响范围是什么") == ""


def test_extract_target_ignores_pure_chinese_punctuation():
    assert extract_target_symbol("修改。会影响谁？") == ""


# --- parse_intent 的候选解析 (硬伤 #1 的完整修法) ---

def test_extract_target_handles_bare_lowercase_name():
    """裸小写函数名必须能抽出: 只认 snake_case/CamelCase 会漏掉大量真实符号。"""
    assert extract_target_symbol("修改 login 会影响谁") == "login"


def test_extract_target_prefers_shaped_over_bare():
    """同时存在 snake_case 与裸词时, snake_case 信息量更大。"""
    assert extract_target_symbol("modify validate_token now") == "validate_token"


def test_target_candidates_are_ordered_by_informativeness():
    from agent import target_candidates

    candidates = target_candidates("修改 `validate_token` 会影响 login")
    assert candidates[0] == "validate_token"   # 反引号最可信
    assert "login" in candidates               # 裸词作为兜底仍在候选里
    assert len(candidates) == len(set(candidates))


def test_resolve_target_verifies_against_graph(live_db):
    """候选必须经过图校验: 普通英文词不得被当成目标。

    live_db 只认 validate_token; 查询 "修改 login 会影响谁" 时 login
    不在图里, 必须退回到能命中的候选而不是硬用 login。
    """
    from agent import resolve_target_symbol

    target, record = resolve_target_symbol("修改 validate_token 会影响谁")
    assert target == "validate_token"
    assert record.get("uid") == "utils.validators:validate_token"
    assert "validate_token" in live_db.calls


def test_resolve_target_skips_candidates_not_in_graph(live_db):
    """图里没有的候选必须被跳过, 否则检索必然落空 (硬伤 #1 的复发形态)。

    场景: 问题里只有一个符号 login, 而图中不存在它。正确行为是**全部
    候选未命中 -> 目标置空**, 而不是硬拿 login 当目标 (那正是硬伤 #1 的
    原始形态: 拿一个不是符号的词去查图)。
    """
    from agent import resolve_target_symbol

    target, record = resolve_target_symbol("修改 login 会影响谁")
    assert target == ""
    assert record.get("error")
    # 候选确实被拿去查过图, 不是直接放弃
    assert "login" in live_db.calls


def test_resolve_target_prefers_hit_over_later_candidate(live_db):
    """多个候选中, 排在前面但图里没有的要被跳过。"""
    from agent import resolve_target_symbol

    # validate_token 是 snake_case (信息量高, 排第一) 且图中存在;
    # login 是裸小写词, 排在其后。
    target, record = resolve_target_symbol("modify validate_token and login")
    assert target == "validate_token"
    assert record["uid"] == "utils.validators:validate_token"


def test_resolve_target_reports_when_nothing_matches(dead_db):
    from agent import resolve_target_symbol

    target, record = resolve_target_symbol("修改 totally_nonexistent_symbol 会影响谁")
    assert record.get("error")
    assert "未找到" in record["error"] or "错误" in record["error"]


def test_dotted_qualname_is_candidate_before_its_head():
    """`AuthService.login` 必须先于 `AuthService` 成为候选。

    30 次真实 LLM 基线暴露: 点分限定名被按单词切成 [AuthService, login],
    排在前面的 AuthService 是 Class, 于是解析到 Class 而非 Method ——
    检索结果是 Class 级调用者, 缺 Method 级, 模型正确回答反被判成幻觉。
    """
    from agent import target_candidates

    candidates = target_candidates("AuthService.login 被谁依赖")
    assert candidates[0] == "AuthService.login"
    assert "AuthService" in candidates          # 单段仍在候选里作兜底


def test_dotted_candidate_resolves_to_method_not_class(live_db):
    """端到端: 点分限定名必须定位到 Method 节点 (label=Method)。"""
    from agent import resolve_target_symbol

    target, record = resolve_target_symbol("AuthService.login 被谁依赖")
    assert target == "AuthService.login"
    assert "Method" in (record.get("labels") or [])
