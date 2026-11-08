"""UP-2 质量门禁测试：预期结果模糊词禁令（词表 + 表述清晰度检查 + 4 样例 0 命中回归）。"""

from __future__ import annotations

import logging

import pytest

from casepilot.core import mock_engine
from casepilot.core.generator import generate_from_file, parse_document
from casepilot.core.quality import (
    FUZZY_WORDS,
    check_clarity,
    find_fuzzy_words,
    format_clarity_report,
)
from casepilot.core.schema import validate_cases
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, PETSTORE_OPENAPI, SECURITY_PERF_PRD

ALL_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD]

# 题目验收要求覆盖的模糊词（至少含这些）
REQUIRED_FUZZY = (
    "或", "可能", "视情况", "大概", "也许", "正常情况", "若干", "等等", "之类", "应该可以", "尽量",
)


def _case_dict(title: str, expected: str) -> dict:
    return {
        "title": title,
        "precondition": "系统可用",
        "steps": "1. 执行操作\n2. 查看结果",
        "expected": expected,
        "priority": "P1",
        "category": "功能",
        "method": "等价类",
        "module": "模块/功能验证",
    }


class TestFuzzyWordList:
    def test_required_words_all_present(self):
        for word in REQUIRED_FUZZY:
            assert word in FUZZY_WORDS, f"模糊词表应包含「{word}」"

    def test_find_fuzzy_words_keeps_order_and_dedup(self):
        assert find_fuzzy_words("登录成功或失败，可能成功，也许失败") == ["或", "可能", "也许"]
        assert find_fuzzy_words("登录成功并跳转首页") == []

    def test_find_fuzzy_words_multihit(self):
        hits = find_fuzzy_words("结果视情况而定，大概 3 秒，尽量保证可用")
        assert hits == ["视情况", "大概", "尽量"]


class TestCheckClarity:
    def test_clean_cases_pass(self):
        report = check_clarity([_case_dict("用例一", "登录成功并跳转首页，提示「欢迎」")])
        assert report.ok
        assert report.issues == []

    def test_fuzzy_case_reports_title_and_words(self):
        """警告明细必须含用例标题 + 命中词（验收断言）。"""
        report = check_clarity(
            [
                _case_dict("验证登录结果", "登录成功或失败，可能跳转首页"),
                _case_dict("验证导出结果", "导出成功并提示「完成」"),
            ]
        )
        assert not report.ok
        assert len(report.issues) == 1
        issue = report.issues[0]
        assert "验证登录结果" in issue
        assert "或" in issue and "可能" in issue

    def test_accepts_pydantic_cases(self):
        cases = validate_cases([_case_dict("用例一", "结果视情况而定")])
        report = check_clarity(cases)
        assert not report.ok
        assert "视情况" in report.issues[0]

    def test_format_report_warning_details(self):
        report = check_clarity([_case_dict("验证超时提示", "超时后大概 5 秒提示重试")])
        text = format_clarity_report(report)
        assert "表述清晰度检查：共 1 条用例（预期结果模糊词禁令）" in text
        assert "警告明细如下" in text
        assert "验证超时提示" in text
        assert "大概" in text
        assert "表述清晰度检查结论：存在模糊表述" in text

    def test_format_report_ok(self):
        report = check_clarity([_case_dict("用例一", "登录成功并跳转首页")])
        text = format_clarity_report(report)
        assert "模糊词命中 0 条（达标）" in text
        assert "表述清晰度检查结论：达标" in text


class TestMockTemplatesFuzzyFree:
    """第一层门禁：mock 生成模板保证不产出模糊词（4 样例回归断言）。"""

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
    def test_generated_expected_have_zero_fuzzy_hits(self, path, offline_env):
        result = generate_from_file(path, mode="mock")
        hits = {
            c.title: find_fuzzy_words(c.expected)
            for c in result.cases
            if find_fuzzy_words(c.expected)
        }
        assert hits == {}, f"{path.name} 生成用例的预期结果命中模糊词：{hits}"

    def test_raw_rule_cases_fuzzy_free(self):
        """直接检查规则引擎原始输出（含补齐模板），双保险。"""
        for path in ALL_SAMPLES:
            profile = parse_document(path)
            for raw in mock_engine.generate(profile):
                assert find_fuzzy_words(raw["expected"]) == [], raw["title"]

    def test_fr_backfill_cases_fuzzy_free(self):
        from casepilot.core.generator import _fr_backfill_cases
        from casepilot.core.schema import FRItem

        for method_text in ("支持导出报表", "防 SQL 注入拦截", "接口响应时间 ≤2 秒"):
            fr = FRItem(id="1", text=method_text)
            for case in _fr_backfill_cases(fr, "模块"):
                assert find_fuzzy_words(case.expected) == [], case.title

    def test_clarity_report_logged_in_generation(self, offline_env, caplog):
        with caplog.at_level(logging.INFO, logger="casepilot"):
            generate_from_file(LOGIN_PRD, mode="mock")
        assert "表述清晰度检查：共" in caplog.text
        assert "表述清晰度检查结论：达标" in caplog.text
