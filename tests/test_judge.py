"""evals/judge.py LLM-as-a-Judge 双模式测试（规则降级 + LLM 路径 httpx 替身）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from casepilot.core import mock_engine
from casepilot.core.generator import parse_document
from casepilot.core.schema import TestCase
from casepilot.exporters.zentao_excel import ZENTAO_HEADERS, export_zentao_excel
from evals.judge import (
    JUDGE_DIMENSIONS,
    judge_cases,
    load_cases_from_excel,
    parse_judge_json,
    resolve_judge_mode,
    rule_review,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
LOGIN_PRD = EXAMPLES / "prd_login.md"


def _write_xlsx(path, rows):
    """按禅道 8 列模板手写测试 Excel（rows 为与 ZENTAO_HEADERS 同序的值列表）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "用例"
    ws.append(ZENTAO_HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def _good_xlsx(tmp_path):
    """用 mock 引擎从登录样例生成一份正常用例 Excel（gen 产出兼容格式）。"""
    profile = parse_document(LOGIN_PRD)
    cases = [TestCase(**raw) for raw in mock_engine.generate(profile)]
    return export_zentao_excel(cases, tmp_path / "good_cases.xlsx")


def _bad_xlsx(tmp_path):
    """坏用例集：缺预期结果 + 优先级全 P0 + 单步步骤。"""
    rows = [
        ["登录模块/验证", "验证登录", "系统可用", "1. 登录", "", "1", "功能", "场景法"],
        ["登录模块/验证", "验证注册", "系统可用", "1. 注册", "", "1", "功能", "场景法"],
        ["登录模块/验证", "验证找回密码", "系统可用", "1. 找回密码", "", "1", "功能", "场景法"],
        ["登录模块/验证", "验证退出登录", "系统可用", "1. 退出", "", "1", "功能", "场景法"],
    ]
    return _write_xlsx(tmp_path / "bad_cases.xlsx", rows)


class TestLoadCasesFromExcel:
    def test_load_good_xlsx(self, tmp_path):
        cases = load_cases_from_excel(_good_xlsx(tmp_path))
        assert len(cases) >= 8
        assert set(cases[0]) == {
            "title", "precondition", "steps", "expected", "priority", "category", "method", "module",
        }

    def test_priority_numeric_mapped_to_p_level(self, tmp_path):
        path = _write_xlsx(
            tmp_path / "p.xlsx",
            [["M/子", "标题一", "前置", "1. 步骤\n2. 步骤", "预期结果", "1", "功能", "等价类"]],
        )
        assert load_cases_from_excel(path)[0]["priority"] == "P0"

    def test_empty_expected_stays_empty(self, tmp_path):
        path = _bad_xlsx(tmp_path)
        assert load_cases_from_excel(path)[0]["expected"] == ""

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="用例文件不存在"):
            load_cases_from_excel(tmp_path / "none.xlsx")

    def test_wrong_sheet_raises_chinese_error(self, tmp_path):
        wb = Workbook()
        wb.active.title = "Sheet1"
        wb.save(tmp_path / "wrong.xlsx")
        with pytest.raises(ValueError, match="缺少「用例」工作表"):
            load_cases_from_excel(tmp_path / "wrong.xlsx")

    def test_no_data_rows_raises(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "用例"
        ws.append(ZENTAO_HEADERS)
        wb.save(tmp_path / "empty.xlsx")
        with pytest.raises(ValueError, match="没有任何用例数据行"):
            load_cases_from_excel(tmp_path / "empty.xlsx")


class TestRuleReview:
    def test_good_set_beats_bad_set_by_margin(self, tmp_path):
        good = rule_review(load_cases_from_excel(_good_xlsx(tmp_path)))
        bad = rule_review(load_cases_from_excel(_bad_xlsx(tmp_path)))
        assert bad.total < 3.0, "坏用例集总分应显著偏低"
        assert good.total - bad.total >= 1.5, "好坏用例集分数应显著拉开"
        for dim in JUDGE_DIMENSIONS:
            assert bad.scores[dim] < good.scores[dim] or good.scores[dim] == 5.0

    def test_missing_expected_hits_completeness_and_executability(self, tmp_path):
        bad = rule_review(load_cases_from_excel(_bad_xlsx(tmp_path)))
        assert bad.scores["完整性"] == 1.0, "预期结果为空 → 完整性 1 分"
        assert bad.scores["可执行性"] == 1.0

    def test_all_p0_hits_priority(self, tmp_path):
        bad = rule_review(load_cases_from_excel(_bad_xlsx(tmp_path)))
        assert bad.scores["优先级合理性"] == 1.0, "优先级全 P0 → 优先级合理性 1 分"

    def test_suggestions_cite_titles_and_fields(self, tmp_path):
        bad = rule_review(load_cases_from_excel(_bad_xlsx(tmp_path)))
        assert len(bad.suggestions) >= 3
        joined = "".join(bad.suggestions)
        assert "验证登录" in joined or "验证注册" in joined, "建议应引用具体用例标题"
        assert "预期结果" in joined
        assert "P0" in joined

    def test_good_set_has_at_least_3_suggestions(self, tmp_path):
        good = rule_review(load_cases_from_excel(_good_xlsx(tmp_path)))
        assert len(good.suggestions) >= 3
        assert all("「" in s for s in good.suggestions), "每条建议应引用具体用例标题"

    def test_report_is_chinese_with_mode_header(self, tmp_path):
        report = rule_review(load_cases_from_excel(_good_xlsx(tmp_path)))
        text = report.format_report("output/prd_login_cases.xlsx")
        assert "评审模式：规则降级" in text
        assert "总分" in text and "## 维度得分" in text and "## 改进建议" in text
        for dim in JUDGE_DIMENSIONS:
            assert dim in text

    def test_deterministic_same_input_same_report(self, tmp_path):
        cases = load_cases_from_excel(_good_xlsx(tmp_path))
        first = rule_review(cases).format_report("a.xlsx")
        second = rule_review(cases).format_report("a.xlsx")
        assert first == second


class FakeResponse:
    """httpx.Response 替身：固定返回指定 content。"""

    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


def _valid_llm_json() -> str:
    return json.dumps(
        {
            "cases": [
                {
                    "title": t,
                    "scores": {d: s for d in JUDGE_DIMENSIONS},
                    "comment": "整体合格",
                }
                for t in ("用例一", "用例二")
                for s in (4,)
            ],
            "suggestions": ["用例「用例一」建议补充边界值", "用例「用例二」建议拆分步骤", "用例「用例一」建议补充前置数据"],
        }
    )


class TestLLMReview:
    def test_llm_mode_aggregates_scores(self, tmp_path, monkeypatch, offline_env):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["body"] = json
            return FakeResponse(_valid_llm_json())

        monkeypatch.setattr("casepilot.core.llm.httpx.post", fake_post)
        cases = load_cases_from_excel(_good_xlsx(tmp_path))
        report = judge_cases(cases, mode="llm")
        assert report.mode == "llm"
        assert "LLM 评审" in report.mode_note
        for dim in JUDGE_DIMENSIONS:
            assert report.scores[dim] == 4.0
        assert len(report.suggestions) >= 3
        body = captured["body"]
        assert body["temperature"] == 0, "LLM 评审必须 temperature=0"
        assert "JSON" in body["messages"][0]["content"]

    def test_parse_failure_retries_with_feedback(self, tmp_path, monkeypatch, offline_env):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            if len(calls) == 1:
                return FakeResponse("好的，我来评审：这不是 JSON")
            return FakeResponse(_valid_llm_json())

        monkeypatch.setattr("casepilot.core.llm.httpx.post", fake_post)
        cases = load_cases_from_excel(_good_xlsx(tmp_path))
        report = judge_cases(cases, mode="llm")
        assert len(calls) == 2
        assert "自动重试" in calls[1]["messages"][1]["content"], "解析失败后错误信息应回灌提示词"
        assert report.mode == "llm"

    def test_persistent_failure_raises_after_retries(self, tmp_path, monkeypatch, offline_env):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return FakeResponse("永远不是 JSON")

        monkeypatch.setattr("casepilot.core.llm.httpx.post", fake_post)
        cases = load_cases_from_excel(_good_xlsx(tmp_path))
        with pytest.raises(ValueError, match="已自动重试 2 次"):
            judge_cases(cases, mode="llm")
        assert len(calls) == 3  # 首次 + 2 次重试

    def test_max_retries_zero_calls_once(self, tmp_path, monkeypatch, offline_env):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
        monkeypatch.setenv("CASEPILOT_LLM_MAX_RETRIES", "0")
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return FakeResponse("不是 JSON")

        monkeypatch.setattr("casepilot.core.llm.httpx.post", fake_post)
        cases = load_cases_from_excel(_good_xlsx(tmp_path))
        with pytest.raises(ValueError, match="已自动重试 0 次"):
            judge_cases(cases, mode="llm")
        assert len(calls) == 1


class TestJudgeCasesDispatch:
    def test_resolve_mode_offline_is_rule(self, offline_env):
        assert resolve_judge_mode() == "rule"

    def test_auto_mode_offline_uses_rule(self, tmp_path, offline_env):
        report = judge_cases(load_cases_from_excel(_good_xlsx(tmp_path)))
        assert report.mode == "rule"

    def test_empty_cases_rejected(self):
        with pytest.raises(ValueError, match="用例集为空"):
            judge_cases([])


class TestParseJudgeJson:
    def test_fence_tolerant(self):
        data = parse_judge_json(f"```json\n{_valid_llm_json()}\n```")
        assert len(data["cases"]) == 2

    def test_invalid_score_rejected(self):
        bad = json.dumps({"cases": [{"title": "t", "scores": {d: 9 for d in JUDGE_DIMENSIONS}}]})
        with pytest.raises(ValueError, match="1-5"):
            parse_judge_json(bad)

    def test_non_json_rejected(self):
        with pytest.raises(ValueError, match="不是合法 JSON"):
            parse_judge_json("随便的文字")
