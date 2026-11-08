"""UP-3 模糊词门禁测试：拒绝 + 回灌重试 + 耗尽丢弃（httpx 替身，全离线）。

验收标准：
- LLM 模式模拟测试（httpx 替身首返含模糊词、重试后干净）→ 断言发生重试且最终 0 模糊词；
- 重试耗尽场景断言用例被丢弃 + 告警输出（列标题 + 命中词）；
- mock 模式 0 命中硬保证回归保留；
- quality / judge 现有模糊词检查逻辑保持一致（词表复用 quality.FUZZY_WORDS）。
"""

from __future__ import annotations

import json

import pytest

from casepilot.core import quality
from casepilot.core.llm import LLMGateway
from casepilot.core.generator import generate_from_file, parse_document
from casepilot.core.quality import (
    FUZZY_WORDS,
    build_fuzzy_feedback,
    check_fuzzy_gate,
    drop_fuzzy_cases,
    find_fuzzy_words,
)
from tests.conftest import (
    BOUNDARY_PRD,
    LOGIN_PRD,
    NUMBERED_PRD,
    PETSTORE_OPENAPI,
    SECURITY_PERF_PRD,
)

ALL_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD, NUMBERED_PRD]


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


CLEAN_EXPECTED = "登录成功并跳转首页，页面显示「欢迎」"
FUZZY_EXPECTED = "登录成功或失败，可能跳转首页，视情况提示错误"


class FakeResponse:
    """httpx.Response 替身：固定返回 content。"""

    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TestFuzzyGateInterface:
    """模糊词门禁阻断接口（quality.check_fuzzy_gate / drop_fuzzy_cases）。"""

    def test_gate_flags_title_and_words(self):
        violations = check_fuzzy_gate([_raw_case("验证登录结果", FUZZY_EXPECTED)])
        assert len(violations) == 1
        assert violations[0].title == "验证登录结果"
        assert set(violations[0].words) == {"或", "可能", "视情况"}

    def test_gate_clean_cases_pass(self):
        assert check_fuzzy_gate([_raw_case("用例一", CLEAN_EXPECTED)]) == []

    def test_feedback_wording(self):
        violations = check_fuzzy_gate([_raw_case("验证登录结果", FUZZY_EXPECTED)])
        feedback = build_fuzzy_feedback(violations)
        assert "预期结果含模糊词" in feedback
        assert "或" in feedback and "可能" in feedback and "视情况" in feedback
        assert "请改写为可验证的确定性表述" in feedback
        assert "验证登录结果" in feedback

    def test_drop_fuzzy_cases_keeps_clean_and_warns(self):
        kept, warnings = drop_fuzzy_cases(
            [
                _raw_case("干净用例", CLEAN_EXPECTED),
                _raw_case("模糊用例", FUZZY_EXPECTED),
            ]
        )
        assert [c["title"] for c in kept] == ["干净用例"]
        assert len(warnings) == 1
        assert "模糊用例" in warnings[0] and "或" in warnings[0], "告警应列标题 + 命中词"

    def test_gate_wordlist_reused_from_quality(self):
        """词表复用：门禁与模糊词检查同用 quality.FUZZY_WORDS（不复制、不加词）。"""
        for word in ("或", "可能", "视情况"):
            assert word in FUZZY_WORDS
        violations = check_fuzzy_gate([_raw_case("t", FUZZY_EXPECTED)])
        assert violations[0].words == find_fuzzy_words(FUZZY_EXPECTED)


class TestLLMFuzzyRetry:
    """LLM 模式：模糊词命中即校验失败，回灌提示词重试（httpx 替身）。"""

    def _run(self, monkeypatch, responses, max_retries=None):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
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

    def test_fuzzy_then_clean_retries_with_feedback(self, monkeypatch, offline_env):
        """验收：首返含模糊词、重试后干净 → 发生重试且最终 0 模糊词。"""
        fuzzy_payload = json.dumps(
            [_raw_case("模糊用例", FUZZY_EXPECTED)], ensure_ascii=False
        )
        clean_payload = json.dumps(
            [_raw_case("干净用例", CLEAN_EXPECTED)], ensure_ascii=False
        )
        calls = self._run(monkeypatch, [fuzzy_payload, clean_payload])
        raw = LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 2, "模糊词命中应恰好触发 1 次重试"
        retry_prompt = calls["bodies"][1]["messages"][1]["content"]
        assert "预期结果含模糊词" in retry_prompt
        assert "或" in retry_prompt and "视情况" in retry_prompt
        assert "请改写为可验证的确定性表述" in retry_prompt
        assert find_fuzzy_words(raw[0]["expected"]) == [], "重试后最终 0 模糊词"
        assert raw[0]["title"] == "干净用例"

    def test_exhausted_retries_drop_fuzzy_and_warn(self, monkeypatch, caplog, offline_env):
        """验收：重试耗尽仍命中的用例被丢弃 + 中文告警（标题 + 命中词）。"""
        import logging

        payload = json.dumps(
            [
                _raw_case("模糊用例", FUZZY_EXPECTED),
                _raw_case("干净用例", CLEAN_EXPECTED),
            ],
            ensure_ascii=False,
        )
        calls = self._run(monkeypatch, [payload], max_retries=1)
        with caplog.at_level(logging.WARNING, logger="casepilot"):
            raw = LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 2, "首返 + 1 次重试后进入丢弃兜底"
        assert [c["title"] for c in raw] == ["干净用例"]
        assert "丢弃" in caplog.text
        assert "模糊用例" in caplog.text and "或" in caplog.text

    def test_all_fuzzy_raises_chinese_error(self, monkeypatch, offline_env):
        payload = json.dumps([_raw_case("模糊用例", FUZZY_EXPECTED)], ensure_ascii=False)
        self._run(monkeypatch, [payload], max_retries=0)
        with pytest.raises(ValueError, match="模糊词门禁"):
            LLMGateway(mode="openai").generate_raw_cases(_profile())

    def test_max_retries_zero_still_drops_once(self, monkeypatch, caplog, offline_env):
        import logging

        payload = json.dumps(
            [
                _raw_case("模糊用例", FUZZY_EXPECTED),
                _raw_case("干净用例", CLEAN_EXPECTED),
            ],
            ensure_ascii=False,
        )
        calls = self._run(monkeypatch, [payload], max_retries=0)
        with caplog.at_level(logging.WARNING, logger="casepilot"):
            raw = LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 1, "重试次数 0 时不重试，直接丢弃兜底"
        assert [c["title"] for c in raw] == ["干净用例"]
        assert "模糊用例" in caplog.text

    def test_clean_output_no_retry(self, monkeypatch, offline_env):
        payload = json.dumps([_raw_case("干净用例", CLEAN_EXPECTED)], ensure_ascii=False)
        calls = self._run(monkeypatch, [payload])
        raw = LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 1
        assert raw[0]["title"] == "干净用例"


class TestMockZeroFuzzyHardGuarantee:
    """mock 模式 0 命中硬保证（回归保留，门禁口径双保险）。"""

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
    def test_mock_output_zero_fuzzy_via_gate(self, path, offline_env):
        result = generate_from_file(path, mode="mock")
        assert check_fuzzy_gate(result.cases) == [], f"{path.name} mock 产物命中模糊词"

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
    def test_fuzzy_words_absent_in_all_expected(self, path, offline_env):
        result = generate_from_file(path, mode="mock")
        for case in result.cases:
            assert find_fuzzy_words(case.expected) == [], case.title
