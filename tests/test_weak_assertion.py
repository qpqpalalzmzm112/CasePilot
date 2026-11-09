"""迭代 9 / UP-3 弱断言禁令测试：词表 + 生成侧回灌重试 / 丢弃 + judge 扣分 + 不误伤（全离线）。

验收标准（DoD 3）：
- 「接口请求返回正常 / 处理成功」类弱断言 → 生成侧校验失败回灌重试，耗尽丢弃 + 中文告警；
- judge 可执行性扣分并建议「写明具体验证点（状态码 / 字段值 / 提示文案 / 界面元素）」；
- 「系统接受有效输入并处理成功」类同义反复预期被 judge 拒收（点名 + 扣分）；
- 不误伤：「命中 0 次」「T0+604800」「≤2s」等具体断言不得命中；
- mock 模板 0 弱断言硬保证（全样例回归）。
"""

from __future__ import annotations

import json

import pytest

from casepilot.core import quality
from casepilot.core.generator import generate_from_file, parse_document
from casepilot.core.llm import LLMGateway
from casepilot.core.quality import (
    WEAK_ASSERTION_PHRASES,
    build_weak_feedback,
    check_weak_gate,
    drop_weak_cases,
    find_tautologies,
    find_weak_assertions,
)
from evals.judge import rule_review
from tests.conftest import (
    BOUNDARY_PRD,
    EXAMPLES_DIR,
    LOGIN_PRD,
    NUMBERED_PRD,
    PETSTORE_OPENAPI,
    SECURITY_PERF_PRD,
)

LOGIN_FULL_PRD = EXAMPLES_DIR / "prd_login_full.md"
ALL_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD, NUMBERED_PRD, LOGIN_FULL_PRD]


def _profile():
    return parse_document(LOGIN_PRD)


def _raw_case(title: str, expected: str) -> dict:
    return {
        "title": title,
        "precondition": "用户已注册且账号状态正常",
        "steps": "1. 打开登录页\n2. 输入账号密码\n3. 点击登录",
        "expected": expected,
        "priority": "P1",
        "category": "功能",
        "method": "等价类",
        "module": "登录/功能验证",
    }


class FakeResponse:
    """httpx.Response 替身：固定返回 content。"""

    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TestWeakAssertionList:
    """弱断言词表：任务指定短语齐全，具体断言不误伤。"""

    def test_required_phrases_present(self):
        for word in ("返回正常", "正常返回", "处理成功", "正常工作", "显示正确", "接口请求返回正常"):
            assert word in WEAK_ASSERTION_PHRASES, f"弱断言词表应包含「{word}」"

    def test_interface_return_normal_flagged(self):
        hits = find_weak_assertions("接口请求返回正常")
        assert "返回正常" in hits and "接口请求返回正常" in hits

    def test_no_false_positives_on_concrete_assertions(self):
        """DoD 3：「命中 0 次」「T0+604800」「≤2s」等具体断言不得命中。"""
        for text in (
            "特征命中 0 次时不触发锁定",
            "订单在 T0+604800 秒过期，T0+604801 秒起不可用",
            "响应时间 ≤2s，吞吐量 ≥100 QPS",
            "提示「验证码已失效，请重新获取」",
        ):
            assert find_weak_assertions(text) == [], text

    def test_tautology_detector_flags_restatement(self):
        """同义反复：「系统接受有效输入并处理成功」「按条款处理正确」无具体验证点。"""
        hits = find_tautologies("系统接受有效输入并处理成功；按条款处理正确")
        assert len(hits) == 2

    def test_tautology_detector_spares_cited_concrete_text(self):
        """带具体验证点（数值 / 提示文案）的表述不算同义反复（迭代 9 / F1）。"""
        assert find_tautologies("锁定 30 分钟，提示「账号已被锁定」") == []
        assert find_tautologies("输入 10000 元（少 1）未达阈值不触发") == []

    def test_tautology_detector_flags_restatement_class(self):
        """迭代 9 / F1：换皮句式（复述条款 + 零可验证行为）整体命中——
        剥离「」引文后无任何具体验证点即判同义反复。"""
        # 「返回结果与「<条款>」的需求定义一致」（坏例句式一）
        hits = find_tautologies(
            "条款「支持账号密码登录」：返回结果与「支持账号密码登录」的需求定义一致"
        )
        assert len(hits) == 1
        # 「系统按「<条款>」的需求规则接受有效输入并完成处理」（坏例句式二）
        hits = find_tautologies(
            "条款「充电电价切换」：系统按「充电电价切换」的需求规则接受有效输入并完成处理"
        )
        assert len(hits) == 1
        # 「系统拒绝「<条款>」的无效等价类输入…返回结果与…拒绝定义一致」（坏例句式三）
        hits = find_tautologies(
            "条款「充电电价切换」：系统拒绝「充电电价切换」的无效等价类输入；"
            "条款「充电电价切换」：返回结果与「充电电价切换」的拒绝定义一致"
        )
        assert len(hits) == 2


class TestWeakGateInterface:
    """弱断言门禁阻断接口（check_weak_gate / drop_weak_cases）。"""

    def test_gate_flags_title_and_phrases(self):
        violations = check_weak_gate([_raw_case("验证接口结果", "接口请求返回正常")])
        assert len(violations) == 1
        assert violations[0].title == "验证接口结果"
        assert "返回正常" in violations[0].phrases

    def test_gate_clean_cases_pass(self):
        assert check_weak_gate([_raw_case("干净用例", "登录成功并跳转首页，页面显示「欢迎」")]) == []

    def test_feedback_wording(self):
        violations = check_weak_gate([_raw_case("验证接口结果", "接口请求返回正常")])
        feedback = build_weak_feedback(violations)
        assert "弱断言" in feedback
        assert "验证接口结果" in feedback
        assert "写明具体验证点（状态码 / 字段值 / 提示文案 / 界面元素）" in feedback

    def test_drop_weak_cases_keeps_clean_and_warns(self):
        kept, warnings = drop_weak_cases(
            [
                _raw_case("干净用例", "登录成功并跳转首页，页面显示「欢迎」"),
                _raw_case("弱断言用例", "接口请求返回正常"),
            ]
        )
        assert [c["title"] for c in kept] == ["干净用例"]
        assert len(warnings) == 1
        assert "弱断言用例" in warnings[0] and "具体验证点" in warnings[0]


class TestLLMWeakRetry:
    """LLM 模式：弱断言命中即校验失败，回灌重试（httpx 替身）。"""

    def _run(self, monkeypatch, responses, max_retries=None):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "casepilot-test-key")
        if max_retries is not None:
            monkeypatch.setenv("CASEPILOT_LLM_MAX_RETRIES", str(max_retries))
        else:
            monkeypatch.delenv("CASEPILOT_LLM_MAX_RETRIES", raising=False)
        calls = {"count": 0, "bodies": []}

        def fake_post(url, headers=None, json=None, timeout=None):
            calls["bodies"].append(json)
            idx = min(calls["count"], len(responses) - 1)
            calls["count"] += 1
            return FakeResponse(responses[idx])

        monkeypatch.setattr("casepilot.core.llm.httpx.post", fake_post)
        return calls

    def test_weak_then_clean_retries_with_feedback(self, monkeypatch, offline_env):
        """DoD 3：首返「接口请求返回正常」→ 回灌重试，重试后干净。"""
        bad_payload = json.dumps(
            [_raw_case("弱断言用例", "接口请求返回正常")], ensure_ascii=False
        )
        good_payload = json.dumps(
            [_raw_case("具体用例", "返回 200 且页面显示「欢迎」")], ensure_ascii=False
        )
        calls = self._run(monkeypatch, [bad_payload, good_payload])
        raw = LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 2, "弱断言命中应恰好触发 1 次重试"
        retry_prompt = calls["bodies"][1]["messages"][1]["content"]
        assert "弱断言" in retry_prompt
        assert "具体验证点" in retry_prompt
        assert raw[0]["title"] == "具体用例"

    def test_exhausted_retries_drop_weak_and_warn(self, monkeypatch, caplog, offline_env):
        """DoD 3：重试耗尽仍弱断言的用例被丢弃 + 中文告警。"""
        import logging

        payload = json.dumps(
            [
                _raw_case("弱断言用例", "接口请求返回正常"),
                _raw_case("干净用例", "登录成功并跳转首页，页面显示「欢迎」"),
            ],
            ensure_ascii=False,
        )
        calls = self._run(monkeypatch, [payload], max_retries=1)
        with caplog.at_level(logging.WARNING, logger="casepilot"):
            raw = LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 2
        assert [c["title"] for c in raw] == ["干净用例"]
        assert "弱断言禁令丢弃用例" in caplog.text
        assert "弱断言用例" in caplog.text

    def test_all_weak_raises_chinese_error(self, monkeypatch, offline_env):
        payload = json.dumps(
            [_raw_case("弱断言用例", "接口请求返回正常")], ensure_ascii=False
        )
        self._run(monkeypatch, [payload], max_retries=0)
        with pytest.raises(ValueError, match="弱断言禁令"):
            LLMGateway(mode="openai").generate_raw_cases(_profile())


class TestJudgeWeakPenalty:
    """judge：弱断言 / 同义反复判低质量并点名，可执行性扣分（DoD 1 / DoD 3）。"""

    def _case(self, expected: str, title: str = "验证登录结果") -> dict:
        return _raw_case(title, expected)

    def test_weak_assertion_deducted_and_named(self):
        report = rule_review([self._case("接口请求返回正常")])
        assert report.scores["可执行性"] <= 2.0
        joined = "".join(report.suggestions)
        assert "验证登录结果" in joined
        assert "弱断言" in joined
        assert "写明具体验证点（状态码 / 字段值 / 提示文案 / 界面元素）" in joined

    def test_tautology_rejected_and_named(self):
        """DoD 1：judge 拒收「按条款处理正确」类同义反复预期（无弱断言短语的独立路径）。"""
        report = rule_review([self._case("按条款处理正确")])
        assert report.scores["可执行性"] <= 2.0
        joined = "".join(report.suggestions)
        assert "同义反复" in joined
        assert "验证登录结果" in joined

    def test_system_accept_tautology_also_rejected(self):
        """DoD 1：「系统接受有效输入并处理成功」类预期被判低质量并点名。"""
        report = rule_review([self._case("系统接受有效输入并处理成功")])
        assert report.scores["可执行性"] <= 2.0
        joined = "".join(report.suggestions)
        assert "验证登录结果" in joined
        assert "弱断言" in joined or "同义反复" in joined

    def test_concrete_assertions_not_penalized(self):
        report = rule_review(
            [self._case("特征命中 0 次时不触发锁定；响应时间 ≤2s，订单在 T0+604800 秒过期")]
        )
        assert report.scores["可执行性"] == 5.0
        assert all("弱断言" not in s for s in report.suggestions)


class TestMockZeroWeakHardGuarantee:
    """mock 模板 0 弱断言 / 0 同义反复硬保证（全样例回归）。"""

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
    def test_mock_output_zero_weak(self, path, offline_env):
        result = generate_from_file(path, mode="mock")
        for case in result.cases:
            assert find_weak_assertions(case.expected) == [], case.title

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
    def test_mock_output_zero_tautology(self, path, offline_env):
        result = generate_from_file(path, mode="mock")
        for case in result.cases:
            assert find_tautologies(case.expected) == [], case.title
