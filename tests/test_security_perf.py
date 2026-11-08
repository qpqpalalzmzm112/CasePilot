"""UP-3 方法论标注校准测试：安全 / 性能测试枚举、识别规则、新样例标注与 3 样例回归（全离线）。"""

from __future__ import annotations

import os
import py_compile
import re
import subprocess
import sys

import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from casepilot.core import mock_engine
from casepilot.core.generator import generate_from_file, parse_document
from casepilot.core.schema import METHODS, TestCase
from casepilot.exporters.markdown_export import build_markdown
from casepilot.exporters.pytest_skeleton import METHOD_MARKS, build_skeleton
from casepilot.exporters.zentao_excel import export_zentao_excel
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, PETSTORE_OPENAPI, SECURITY_PERF_PRD

# 迭代 6 之前的基线（用例数 + 方法论 / 用例类型分布）：回归断言硬编码，保证零漂移
REGRESSION_BASELINE = {
    "prd_login.md": {
        "count": 27,
        "methods": {"等价类": 12, "边界值": 10, "场景法": 3, "判定表": 2},
        "categories": {"功能": 9, "异常": 8, "边界": 10},
    },
    "openapi_petstore.yaml": {
        "count": 30,
        "methods": {"等价类": 12, "边界值": 11, "场景法": 5, "判定表": 2},
        "categories": {"功能": 2, "异常": 8, "接口": 9, "边界": 11},
    },
    "prd_boundary.md": {
        "count": 25,
        "methods": {"等价类": 10, "边界值": 10, "场景法": 3, "判定表": 2},
        "categories": {"功能": 8, "异常": 7, "边界": 10},
    },
}

OLD_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD]


def _case(**overrides) -> dict:
    base = {
        "title": "验证登录成功",
        "precondition": "用户已注册",
        "steps": "1. 输入账号密码\n2. 点击登录",
        "expected": "登录成功并跳转首页",
        "priority": "P1",
        "category": "功能",
        "method": "安全测试",
        "module": "登录模块/安全验证",
    }
    base.update(overrides)
    return base


class TestSchemaEnum:
    def test_new_methods_accepted(self):
        assert TestCase.model_validate(_case(method="安全测试")).method == "安全测试"
        assert TestCase.model_validate(_case(method="性能测试")).method == "性能测试"

    def test_methods_tuple_extended(self):
        assert METHODS == ("等价类", "边界值", "场景法", "判定表", "安全测试", "性能测试")

    def test_invalid_method_still_rejected(self):
        for bad in ("安全", "性能", "安全测试用例", "压测", ""):
            with pytest.raises(ValidationError):
                TestCase.model_validate(_case(method=bad))


class TestClassifyRequirement:
    def test_strong_security_words(self):
        for text in ("防 SQL 注入拦截", "敏感信息脱敏展示", "越权访问拦截", "会话令牌防篡改", "防御攻击测试"):
            assert mock_engine.classify_requirement(text) == "安全测试", text

    def test_perf_words(self):
        for text in ("接口响应时间 ≤2 秒", "支持 50 并发用户同时登录", "登录接口吞吐量 ≥100 QPS", "压力场景持续 30 分钟", "页面加载耗时 ≤3 秒"):
            assert mock_engine.classify_requirement(text) == "性能测试", text

    def test_soft_words_without_context_not_classified(self):
        """泛化词出现在普通功能需求里不得误判（保护原有样例方法论分布）。"""
        assert mock_engine.classify_requirement("支持短信验证码登录", "功能清单") is None
        assert mock_engine.classify_requirement("支持图形验证码校验", "功能清单") is None
        assert mock_engine.classify_requirement("5 次锁定账号 30 分钟", "业务规则") is None

    def test_soft_words_with_section_context(self):
        assert mock_engine.classify_requirement("登录失败 5 次锁定账号 30 分钟", "安全要求") == "安全测试"
        assert mock_engine.classify_requirement("短信验证码 60 秒内有效", "安全要求") == "安全测试"

    def test_soft_words_with_constraint_verb(self):
        assert mock_engine.classify_requirement("防止验证码被暴力破解") == "安全测试"

    def test_unrelated_text_returns_none(self):
        assert mock_engine.classify_requirement("支持账号密码登录", "功能清单") is None


class TestSecurityPerfSample:
    """验收：新样例安全 / 性能相关用例标注比例 ≥90%。"""

    def setup_method(self):
        self.profile = parse_document(SECURITY_PERF_PRD)
        self.result = generate_from_file(SECURITY_PERF_PRD, mode="mock")

    def test_fr_classification(self):
        classified = [
            fr for fr in self.profile.frs
            if mock_engine.classify_requirement(fr.text, fr.section)
        ]
        assert len(classified) == len(self.profile.frs) == 11, "新样例 FR 应全部是安全 / 性能类需求"

    def test_security_perf_label_ratio_ge_90(self):
        class_frs = [
            fr for fr in self.profile.frs
            if mock_engine.classify_requirement(fr.text, fr.section)
        ]
        related = [c for c in self.result.cases if any(fr.tag in c.title for fr in class_frs)]
        labeled = [c for c in related if c.method in ("安全测试", "性能测试")]
        ratio = len(labeled) / len(related)
        assert related, "应存在与安全 / 性能 FR 关联的用例"
        assert ratio >= 0.9, f"安全 / 性能标注比例仅 {ratio:.0%}（{len(labeled)}/{len(related)}）"
        methods = {c.method for c in related}
        assert "安全测试" in methods and "性能测试" in methods

    def test_no_more_forced_boundary_labels(self):
        """性能类需求不再硬套「边界值」：含性能指标词的用例必须标注性能测试。"""
        perf_words = ("响应时间", "并发", "吞吐", "耗时", "QPS", "压力")
        hits = [
            c for c in self.result.cases
            if any(w in c.title for w in perf_words)
        ]
        assert hits, "应存在引用性能指标的用例"
        for c in hits:
            assert c.method == "性能测试", f"{c.title} 不应硬套边界值等标注"

    def test_boundary_derived_cases_carry_specialized_labels(self):
        """数字边界 / 功能点生成的用例按需求语境标注（响应时间 ≤2 秒 → 性能测试）。"""
        by_title = {c.title: c for c in self.result.cases}
        perf_cases = [c for t, c in by_title.items() if "接口响应时间" in t]
        assert perf_cases
        assert all(c.method == "性能测试" for c in perf_cases)
        # 排除场景法 / 判定表（标题会引用主流程步骤文本，与需求条目标注无关）
        sec_cases = [
            c for t, c in by_title.items()
            if ("锁定" in t or "验证码" in t) and c.method not in ("场景法", "判定表")
        ]
        assert sec_cases
        assert all(c.method == "安全测试" for c in sec_cases)

    def test_sample_cases_are_valid_and_tagged(self):
        for c in self.result.cases:
            assert isinstance(c, TestCase)
        # 每个 FR 至少 1 条带追溯前缀的用例
        for fr in self.profile.frs:
            assert any(fr.tag in c.title for c in self.result.cases), fr.tag


class TestRegressionThreeSamples:
    """原有 3 样例回归：用例数与方法论 / 类型分布与迭代 6 前完全一致。"""

    @pytest.mark.parametrize("path", OLD_SAMPLES, ids=lambda p: p.stem)
    def test_distribution_unchanged(self, path, offline_env):
        expected = REGRESSION_BASELINE[path.name]
        result = generate_from_file(path, mode="mock")
        methods: dict[str, int] = {}
        categories: dict[str, int] = {}
        for c in result.cases:
            methods[c.method] = methods.get(c.method, 0) + 1
            categories[c.category] = categories.get(c.category, 0) + 1
        assert len(result.cases) == expected["count"]
        assert methods == expected["methods"]
        assert categories == expected["categories"]

    def test_no_security_perf_labels_in_old_samples(self, offline_env):
        for path in OLD_SAMPLES:
            result = generate_from_file(path, mode="mock")
            for c in result.cases:
                assert c.method not in ("安全测试", "性能测试"), f"{path.name} 不应触发安全 / 性能标注"


class TestPytestMarks:
    def test_method_marks_mapping_extended(self):
        assert METHOD_MARKS["安全测试"] == "security"
        assert METHOD_MARKS["性能测试"] == "performance"

    def test_skeleton_marks_for_security_perf_sample(self, tmp_path, offline_env):
        result = generate_from_file(SECURITY_PERF_PRD, mode="mock")
        text = build_skeleton(result.cases, source=str(SECURITY_PERF_PRD), generated_at="2026-01-01 00:00:00")
        assert "@pytest.mark.security" in text
        assert "@pytest.mark.performance" in text
        assert "# 方法: 安全测试" in text
        assert "# 方法: 性能测试" in text
        out = tmp_path / "sec_perf.py"
        out.write_text(text, encoding="utf-8")
        py_compile.compile(str(out), doraise=True)

    def test_collect_only_zero_error(self, tmp_path, offline_env):
        result = generate_from_file(SECURITY_PERF_PRD, mode="mock")
        out = tmp_path / "sec_perf.py"
        out.write_text(
            build_skeleton(result.cases, source=str(SECURITY_PERF_PRD), generated_at="2026-01-01 00:00:00"),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider",
             "--disable-warnings", str(out)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path.parent),
        )
        assert proc.returncode == 0, f"收集失败：\n{proc.stdout}\n{proc.stderr}"
        # 剔除临时目录名再断言（目录名可能含 error 字样造成假阳性）
        combined = (proc.stdout + proc.stderr).replace(tmp_path.name, "tmpdir")
        assert "error" not in combined.lower(), f"存在收集错误：\n{combined}"
        assert len(re.findall(r"test_\d{3}_", combined)) >= 10


class TestExportCompatibility:
    def test_zentao_keyword_column_carries_new_labels(self, tmp_path, offline_env):
        result = generate_from_file(SECURITY_PERF_PRD, mode="mock")
        out = tmp_path / "sec_perf.xlsx"
        export_zentao_excel(result.cases, out)
        ws = load_workbook(out)["用例"]
        keywords = {str(ws.cell(row=r, column=8).value) for r in range(2, ws.max_row + 1)}
        assert "安全测试" in keywords and "性能测试" in keywords
        # 8 列格式不变
        assert ws.max_column == 8

    def test_markdown_stats_include_new_methods(self, offline_env):
        result = generate_from_file(SECURITY_PERF_PRD, mode="mock")
        text = build_markdown(result.cases, source=str(SECURITY_PERF_PRD))
        assert "| 安全测试 |" in text
        assert "| 性能测试 |" in text
