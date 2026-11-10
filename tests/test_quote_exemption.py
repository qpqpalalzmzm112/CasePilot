"""迭代 10 / UP-3 引文豁免校准 + UP-4 模型分级测试（全离线）。

UP-3 验收口径（DoD 3）：
- 「用户名或密码错误」提示原文三引用形态（「」/ 无引号提示动词后 / ""等其余引号）
  模糊词命中 0；
- 真含糊表述照常命中（「结果正确或失败均返回」的「或」）；
- judge 模糊词扣分同步校准（引文内「或」不扣分，含糊「或」照常扣）。

UP-4 验收口径（DoD 4）：
- CASEPILOT_JUDGE_MODEL 独立生效并回退 CASEPILOT_LLM_MODEL；
- judge 运行日志明示当前评审模型；README 环境变量同步。
"""

from __future__ import annotations

import logging

import pytest

from casepilot.core import quality
from casepilot.core.llm import get_judge_model, get_model
from casepilot.core.quality import (
    find_fuzzy_words,
    find_measurement_predictions,
    strip_quotes,
)
from evals.judge import _score_executability, llm_review

# 登录提示原文（真实 PRD 常见形态）：内含「或」但属提示原文逐字引用
PROMPT_TEXT = "用户名或密码错误"

# 三引用形态
FORM_CORNER = f"提示「{PROMPT_TEXT}」，登录失败停留在登录页"
FORM_DOUBLE_CORNER = f"提示『{PROMPT_TEXT}』，登录失败停留在登录页"
FORM_NO_QUOTE = f"报错为{PROMPT_TEXT}，登录失败停留在登录页"
FORM_NO_QUOTE_SHOW = f"显示{PROMPT_TEXT}"
FORM_NO_QUOTE_HINT = f"提示{PROMPT_TEXT}"
FORM_ASCII_DOUBLE = f'提示语为"{PROMPT_TEXT}"，登录失败'
FORM_CURLY_DOUBLE = f"文案为“{PROMPT_TEXT}”，登录失败"
FORM_ASCII_SINGLE = f"提示'{PROMPT_TEXT}'，登录失败"
FORM_CURLY_SINGLE = f"显示‘{PROMPT_TEXT}’"

ALL_EXEMPT_FORMS = [
    FORM_CORNER,
    FORM_DOUBLE_CORNER,
    FORM_NO_QUOTE,
    FORM_NO_QUOTE_SHOW,
    FORM_NO_QUOTE_HINT,
    FORM_ASCII_DOUBLE,
    FORM_CURLY_DOUBLE,
    FORM_ASCII_SINGLE,
    FORM_CURLY_SINGLE,
]

# 真含糊表述（无提示动词、非引文）：照常命中
AMBIGUOUS_OR = "结果正确或失败均返回"
AMBIGUOUS_SUBMIT = "输入合法或非法均可提交"


def _judge_case(expected: str) -> dict:
    return {
        "title": "验证登录失败提示",
        "precondition": "用户已注册且账号状态正常",
        "steps": "1. 打开登录页\n2. 输入错误密码\n3. 点击登录",
        "expected": expected,
        "priority": "P1",
        "category": "功能",
        "method": "等价类",
        "module": "登录/功能验证",
    }


class TestQuoteExemptionAllForms:
    """UP-3：「」『』""'' 全部引号形态 + 无引号提示原文豁免，登录 6 条误报根治。"""

    @pytest.mark.parametrize("text", ALL_EXEMPT_FORMS)
    def test_prompt_text_forms_zero_fuzzy(self, text):
        assert find_fuzzy_words(text) == [], f"提示原文引用不应判模糊词：{text}"

    def test_no_quote_prompt_verb_forms(self):
        """无引号提示原文：提示 / 显示 / 报错为 / 提示语为 / 文案为 动词后至标点豁免。"""
        for text in (
            "提示用户名或密码错误，登录失败",
            "显示用户名或密码错误",
            "报错为用户名或密码错误",
            "提示语为用户名或密码错误",
            "文案为用户名或密码错误",
            "提示文案为用户名或密码错误",
        ):
            assert find_fuzzy_words(text) == [], text

    def test_ambiguous_or_still_hit(self):
        """真含糊「或」照常命中（引文豁免不误放含糊判定）。"""
        assert find_fuzzy_words(AMBIGUOUS_OR) == ["或"]
        assert find_fuzzy_words(AMBIGUOUS_SUBMIT) == ["或"]

    def test_ambiguous_after_verb_segment_still_scoped(self):
        """豁免只覆盖提示动词后至标点的提示原文片段，其他片段照常判定。"""
        # 「提示用户名或密码错误」是提示原文（豁免），分号后的「结果成功或失败」是含糊判定（命中）
        mixed = "提示用户名或密码错误；结果成功或失败均返回"
        assert find_fuzzy_words(mixed) == ["或"]

    def test_quote_spans_cover_all_pairs(self):
        spans = quality.quote_spans("「一」『二』“三”‘四’\"五\"'六'")
        assert len(spans) == 6

    def test_strip_quotes_removes_prompt_segments(self):
        assert "或" not in strip_quotes(f"报错为{PROMPT_TEXT}")

    def test_measurement_prediction_prompt_quote_exempt(self):
        """提示原文内的指标词同样豁免（引文豁免口径统一）。"""
        assert find_measurement_predictions("提示「平均响应时间为 2 秒」，登录成功") == []
        assert find_measurement_predictions("平均响应时间为 2 秒") != []


class TestLoginCaseSetZeroFuzzy:
    """UP-3 回归：含提示原文的登录用例集模糊词命中 0（check_clarity / 门禁同口径）。"""

    def test_case_set_zero_fuzzy(self):
        cases = [
            _judge_case(FORM_CORNER),
            _judge_case(FORM_NO_QUOTE),
            _judge_case(FORM_ASCII_DOUBLE),
        ]
        report = quality.check_clarity(cases)
        assert report.ok, report.issues
        assert quality.check_fuzzy_gate(cases) == []

    def test_ambiguous_case_set_still_blocked(self):
        cases = [_judge_case(AMBIGUOUS_OR)]
        violations = quality.check_fuzzy_gate(cases)
        assert len(violations) == 1 and "或" in violations[0].words


class TestJudgeSyncCalibrated:
    """UP-3：judge 模糊词扣分同步校准（词表与豁免口径 import 复用）。"""

    @pytest.mark.parametrize("text", ALL_EXEMPT_FORMS)
    def test_judge_not_penalized_for_prompt_text(self, text):
        score, issue = _score_executability(_judge_case(text))
        assert "模糊词" not in (issue or ""), f"提示原文不应扣模糊词分：{text}"
        assert score >= 3, f"提示原文引用不应因模糊词降到 2 分：{text}"

    def test_judge_still_penalizes_ambiguous_or(self):
        score, issue = _score_executability(_judge_case(AMBIGUOUS_OR))
        assert score == 2
        assert issue and "模糊词" in issue and "或" in issue


class TestJudgeModelTiering:
    """UP-4：CASEPILOT_JUDGE_MODEL 独立生效、回退 CASEPILOT_LLM_MODEL、日志明示。"""

    def test_judge_model_env_independent(self, monkeypatch):
        monkeypatch.setenv("CASEPILOT_JUDGE_MODEL", "judge-model-x")
        monkeypatch.setenv("CASEPILOT_LLM_MODEL", "gen-model-y")
        assert get_judge_model("openai") == "judge-model-x"

    def test_judge_model_falls_back_to_llm_model(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_JUDGE_MODEL", raising=False)
        monkeypatch.setenv("CASEPILOT_LLM_MODEL", "gen-model-y")
        assert get_judge_model("openai") == "gen-model-y"
        assert get_judge_model("openai") == get_model("openai")

    def test_judge_model_default_without_env(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_JUDGE_MODEL", raising=False)
        monkeypatch.delenv("CASEPILOT_LLM_MODEL", raising=False)
        assert get_judge_model("openai") == get_model("openai")

    def test_llm_review_uses_judge_model_and_logs(self, monkeypatch, caplog):
        """casepilot judge 终审走 CASEPILOT_JUDGE_MODEL，运行日志明示当前评审模型。"""
        import json as _json

        captured: list[dict] = []

        def fake_call(base_url, api_key, model, system, user):
            captured.append({"model": model})
            return _json.dumps(
                {
                    "cases": [
                        {
                            "title": "t",
                            "scores": {
                                "完整性": 5,
                                "方法论标注合理性": 5,
                                "优先级合理性": 5,
                                "可执行性": 5,
                            },
                            "comment": "ok",
                        }
                    ],
                    "suggestions": ["建议一", "建议二", "建议三"],
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr("casepilot.core.llm.call_chat_completions", fake_call)
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "test-fake-key")
        monkeypatch.setenv("CASEPILOT_JUDGE_MODEL", "judge-model-x")
        monkeypatch.delenv("CASEPILOT_LLM_MODEL", raising=False)
        with caplog.at_level(logging.INFO, logger="casepilot"):
            report = llm_review([_judge_case("登录成功并跳转首页")])
        assert captured[0]["model"] == "judge-model-x", "终审应使用独立评审模型"
        assert "当前评审模型：judge-model-x" in caplog.text
        assert "judge-model-x" in report.mode_note

    def test_judge_model_fallback_used_by_review(self, monkeypatch):
        import json as _json

        captured: list[dict] = []

        def fake_call(base_url, api_key, model, system, user):
            captured.append({"model": model})
            return _json.dumps(
                {
                    "cases": [
                        {
                            "title": "t",
                            "scores": {
                                "完整性": 5,
                                "方法论标注合理性": 5,
                                "优先级合理性": 5,
                                "可执行性": 5,
                            },
                            "comment": "ok",
                        }
                    ],
                    "suggestions": ["建议一", "建议二", "建议三"],
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr("casepilot.core.llm.call_chat_completions", fake_call)
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "test-fake-key")
        monkeypatch.delenv("CASEPILOT_JUDGE_MODEL", raising=False)
        monkeypatch.setenv("CASEPILOT_LLM_MODEL", "gen-model-y")
        llm_review([_judge_case("登录成功并跳转首页")])
        assert captured[0]["model"] == "gen-model-y", "未设置评审模型时应回退 CASEPILOT_LLM_MODEL"


class TestReadmeSync:
    """UP-4：README 环境变量表与快速配置补 CASEPILOT_JUDGE_MODEL 说明。"""

    def test_readme_documents_judge_model(self):
        readme = (__import__("pathlib").Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        assert "CASEPILOT_JUDGE_MODEL" in readme
