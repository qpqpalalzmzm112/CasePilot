"""UP-2 幻觉溯源检查测试：推测项标记 / 规则改写拦截 / --strict 删除 / mock 无编造规则（全离线）。

验收标准：
- 虚构规则坏例（限流降级型）→ judge 标「推测项」并给出建议（引用标题与片段）；
- 规则改写坏例（30 分钟 → 30 秒型）→ 被拦截；
- --strict 删除生效（用例被移出评审集并告警）；
- mock 引擎对样例输出全文检索无 PRD 依据外的编造规则（代码断言）；
- LLM 模式链路单测（httpx 替身返回编造内容 → 被溯源检查拦下）。
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from casepilot.cli import app
from casepilot.core import quality
from casepilot.core.llm import LLMGateway
from casepilot.core.generator import generate_from_file, parse_document
from casepilot.exporters.zentao_excel import export_zentao_excel
from evals.judge import judge_cases, run_grounding_check
from tests.conftest import (
    BOUNDARY_PRD,
    LOGIN_PRD,
    NUMBERED_PRD,
    PETSTORE_OPENAPI,
    SECURITY_PERF_PRD,
)

runner = CliRunner()
ALL_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD, NUMBERED_PRD]

# 编号样例中 NFR-1.3 条款（规则改写坏例的原文依据）
LOCK_CLAUSE = "同一账号连续错误 5 次后锁定领取 30 分钟"


def _case(**overrides) -> dict:
    base = {
        "title": "[NFR-1.3] 验证连续错误限制",
        "precondition": "账号状态正常",
        "steps": "1. 连续错误 5 次后再次领取优惠券\n2. 观察领取结果",
        "expected": "连续错误 5 次后锁定领取 30 分钟，锁定期间提示「账号已锁定」",
        "priority": "P2",
        "category": "异常",
        "method": "安全测试",
        "module": "优惠券系统/异常验证",
    }
    base.update(overrides)
    return base


def _combined(result) -> str:
    """兼容 click 不同版本的输出捕获方式。"""
    out = result.output or ""
    try:
        out += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    return out


class TestExtractNumericTokens:
    def test_number_with_unit(self):
        assert quality.extract_numeric_tokens("锁定 30 分钟") == {(30.0, "分钟")}

    def test_range_both_ends(self):
        tokens = quality.extract_numeric_tokens("密码长度 8-20 位")
        assert (8.0, "位") in tokens and (20.0, "位") in tokens

    def test_bare_number_recorded_with_empty_unit(self):
        assert (8.0, "") in quality.extract_numeric_tokens("minLength: 8")

    def test_step_numbers_and_fr_labels_excluded(self):
        text = "1. 打开页面\nFR-1.1：支持登录\nNFR-1.2：响应时间 2 秒"
        tokens = quality.extract_numeric_tokens(text)
        assert (1.0, "") not in tokens
        assert (1.1, "") not in tokens
        assert (1.2, "") not in tokens
        assert (2.0, "秒") in tokens


class TestGroundingRules:
    """溯源判定规则：数字 ±1 推导豁免；实词命中 ≥2 算有依据。"""

    def setup_method(self):
        self.source = NUMBERED_PRD.read_text(encoding="utf-8")
        self.frs = parse_document(NUMBERED_PRD).frs

    def test_clean_case_passes(self):
        report = quality.check_grounding([_case()], self.source, self.frs)
        assert report.ok, [i.fragment for i in report.items]

    def test_boundary_derived_values_exempt(self):
        """边界 ±1 类测试推导值豁免溯源（属合法推导）。"""
        case = _case(expected="连续错误 5 次后锁定领取 30 分钟；输入 4 次（少 1）未达阈值不触发，输入 6 次（多 1）触发限制")
        report = quality.check_grounding([case], self.source, self.frs)
        assert report.ok, [i.fragment for i in report.items]

    def test_fabricated_rule_marked_as_speculative(self):
        """虚构规则（PRD 无「限流降级」）→ 标记推测项，建议引用标题与片段。"""
        case = _case(
            expected="连续错误 5 次后锁定领取 30 分钟；触发限流降级，服务进入只读模式",
        )
        report = quality.check_grounding([case], self.source, self.frs)
        assert not report.ok
        kinds = {i.kind for i in report.items}
        assert quality.KIND_ACTION in kinds
        joined = "".join(i.suggestion for i in report.items)
        assert "触发限流降级" in joined, "建议应引用无依据片段"
        assert "[NFR-1.3] 验证连续错误限制" in joined, "建议应引用用例标题"
        assert "推测项" in report.format_report()

    def test_rule_rewrite_30_seconds_intercepted(self):
        """规则改写（PRD「锁定 30 分钟」→ 用例「30 秒」）→ 数字断言被拦截。"""
        case = _case(expected="连续错误 5 次后锁定领取 30 秒，30 秒后自动解锁")
        report = quality.check_grounding([case], self.source, self.frs)
        assert not report.ok
        numeric = [i for i in report.items if i.kind == quality.KIND_NUMERIC]
        assert numeric, "「30 秒」改写应按数字/阈值断言拦截"
        joined = "".join(i.suggestion for i in numeric)
        assert "30秒" in joined
        assert "疑似规则改写" in joined
        assert "对应条款「" + LOCK_CLAUSE in joined or LOCK_CLAUSE in joined

    def test_rule_rewrite_other_unit_still_cited(self):
        """改写数值单位同样拦截（如 5 次 → 5 个，阈值语义被改写）。"""
        case = _case(expected="连续错误 5 个后锁定领取 30 分钟")
        report = quality.check_grounding([case], self.source, self.frs)
        assert any(i.kind == quality.KIND_NUMERIC for i in report.items)

    def test_fr_clause_preferred_in_suggestion(self):
        """同一 [FR-x] 条款优先：建议引用对应条款文本。"""
        case = _case(title="[NFR-1.3] 验证锁定", expected="触发限流降级")
        report = quality.check_grounding([case], self.source, self.frs)
        assert report.items
        assert LOCK_CLAUSE in report.items[0].suggestion

    def test_report_section_names_title_and_fragment(self):
        case = _case(expected="触发限流降级")
        report = quality.check_grounding([case], self.source, self.frs)
        text = report.format_report()
        assert "## 幻觉溯源校验：推测项" in text
        assert "触发限流降级" in text
        assert "[NFR-1.3] 验证连续错误限制" in text

    def test_deterministic(self):
        case = _case(expected="触发限流降级；锁定 30 秒")
        first = quality.check_grounding([case], self.source, self.frs).format_report()
        second = quality.check_grounding([case], self.source, self.frs).format_report()
        assert first == second


class TestMockEngineNoFabricatedRules:
    """验收：mock 引擎对样例输出全文检索无 PRD 依据外的编造规则（代码断言）。"""

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
    def test_mock_raw_cases_fully_grounded(self, path):
        from casepilot.core import mock_engine

        profile = parse_document(path)
        source = path.read_text(encoding="utf-8")
        report = quality.check_grounding(mock_engine.generate(profile), source, profile.frs)
        assert report.ok, f"{path.name} mock 输出存在无依据断言：{[i.fragment for i in report.items]}"

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
    def test_generated_cases_fully_grounded(self, path, offline_env):
        """生成全流程输出（含 FR 补齐用例）同样不得引入 PRD 外规则。"""
        result = generate_from_file(path, mode="mock")
        source = path.read_text(encoding="utf-8")
        report = quality.check_grounding(result.cases, source, parse_document(path).frs)
        assert report.ok, f"{path.name} 生成输出存在无依据断言：{[i.fragment for i in report.items]}"

    def test_mock_engine_templates_free_of_fabricated_phrases(self):
        """mock 引擎模板不得残留编造规则表述（限流降级 / 脏数据 类）。"""
        import inspect

        from casepilot.core import mock_engine

        source = inspect.getsource(mock_engine)
        for banned in ("限流降级", "熔断", "不产生脏数据", "事务回滚", "数据不被写入"):
            assert banned not in source, f"mock 引擎残留编造规则表述「{banned}」"


class TestJudgeGroundingWiring:
    """judge --source 溯源校验：推测项标记与 --strict 删除。"""

    def _write_xlsx(self, tmp_path, rows):
        from casepilot.exporters.zentao_excel import ZENTAO_HEADERS
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "用例"
        ws.append(ZENTAO_HEADERS)
        for row in rows:
            ws.append(row)
        out = tmp_path / "cases.xlsx"
        wb.save(out)
        return out

    def test_run_grounding_check_marks_speculative(self, tmp_path):
        rows = [
            _case(),
            _case(title="[NFR-1.3] 验证虚构规则", expected="连续错误 5 次后锁定领取 30 分钟；触发限流降级"),
        ]
        kept, report = run_grounding_check(rows, NUMBERED_PRD)
        assert len(kept) == 2, "非严格模式不删除用例"
        assert any("触发限流降级" in i.fragment for i in report.items)
        assert report.speculative_indexes == {2}

    def test_strict_deletes_speculative_cases(self, tmp_path):
        rows = [
            _case(),
            _case(title="[NFR-1.3] 验证虚构规则", expected="触发限流降级，服务降级只读"),
        ]
        kept, report = run_grounding_check(rows, NUMBERED_PRD, strict=True)
        assert [c["title"] for c in kept] == ["[NFR-1.3] 验证连续错误限制"]
        assert report.removed, "--strict 应删除推测项用例并记入报告"
        assert not report.items
        text = report.format_report()
        assert "严格模式" in text
        assert "已删除" in text

    def test_strict_deletes_rule_rewrite_case(self):
        rows = [_case(title="[NFR-1.3] 验证锁定 30 秒", expected="连续错误 5 次后锁定领取 30 秒")]
        kept, report = run_grounding_check(rows, NUMBERED_PRD, strict=True)
        assert kept == []
        assert report.removed

    def test_judge_report_contains_grounding_section(self):
        rows = [_case(expected="连续错误 5 次后锁定领取 30 分钟；触发限流降级")]
        _, report = run_grounding_check(rows, NUMBERED_PRD)
        judged = judge_cases(rows, mode="rule", grounding=report)
        text = judged.format_report("cases.xlsx")
        assert "幻觉溯源校验：推测项" in text
        assert "触发限流降级" in text

    def test_missing_source_file_raises(self):
        with pytest.raises(FileNotFoundError, match="溯源依据文件不存在"):
            run_grounding_check([_case()], "不存在的.md")


class TestCliJudgeSource:
    """CLI judge --source / --strict 集成。"""

    def _good_xlsx(self, tmp_path, offline_env):
        result = generate_from_file(NUMBERED_PRD, mode="mock")
        return export_zentao_excel(result.cases, tmp_path / "good.xlsx")

    def _bad_xlsx(self, tmp_path):
        from casepilot.exporters.zentao_excel import ZENTAO_HEADERS
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "用例"
        ws.append(ZENTAO_HEADERS)
        for case in (
            _case(),
            _case(
                title="[NFR-1.3] 验证虚构规则",
                expected="连续错误 5 次后锁定领取 30 分钟；触发限流降级",
            ),
        ):
            ws.append(
                [
                    case["module"], case["title"], case["precondition"], case["steps"],
                    case["expected"], 3, case["category"], case["method"],
                ]
            )
        out = tmp_path / "bad.xlsx"
        wb.save(out)
        return out

    def test_good_cases_show_zero_speculative(self, tmp_path, offline_env):
        cases = self._good_xlsx(tmp_path, offline_env)
        result = runner.invoke(app, ["judge", str(cases), "--source", str(NUMBERED_PRD)])
        assert result.exit_code == 0, _combined(result)
        text = _combined(result)
        assert "幻觉溯源校验：推测项" in text
        assert "无推测项" in text

    def test_bad_case_marked_with_title_and_fragment(self, tmp_path, offline_env):
        cases = self._bad_xlsx(tmp_path)
        result = runner.invoke(app, ["judge", str(cases), "--source", str(NUMBERED_PRD)])
        assert result.exit_code == 0, _combined(result)
        text = _combined(result)
        assert "推测项" in text
        assert "触发限流降级" in text
        assert "[NFR-1.3] 验证虚构规则" in text

    def test_strict_deletes_and_warns(self, tmp_path, offline_env):
        cases = self._bad_xlsx(tmp_path)
        result = runner.invoke(
            app, ["judge", str(cases), "--source", str(NUMBERED_PRD), "--strict"]
        )
        assert result.exit_code == 0, _combined(result)
        text = _combined(result)
        assert "严格模式告警" in text
        assert "已删除推测项用例" in text
        assert "触发限流降级" in text

    def test_strict_without_source_rejected(self, tmp_path, offline_env):
        cases = self._bad_xlsx(tmp_path)
        result = runner.invoke(app, ["judge", str(cases), "--strict"])
        assert result.exit_code == 1
        assert "--strict 需与 --source 联用" in _combined(result)

    def test_judge_help_mentions_source(self):
        result = runner.invoke(app, ["judge", "--help"])
        text = _combined(result)
        assert "--source" in text and "--strict" in text
        assert "推测项" in text


class TestLLMChainInterception:
    """LLM 模式链路：httpx 替身返回编造内容 → 被溯源检查拦下。"""

    def test_llm_fabricated_output_flagged_by_grounding(self, monkeypatch, offline_env):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
        fabricated = [
            _case(
                title="[NFR-1.3] 验证限流降级",
                expected="连续错误 5 次后锁定领取 30 秒；触发限流降级并熔断服务",
            )
        ]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(fabricated, ensure_ascii=False)}}]}

        monkeypatch.setattr("casepilot.core.llm.httpx.post", lambda *a, **k: FakeResponse())
        profile = parse_document(NUMBERED_PRD)
        raw = LLMGateway(mode="openai").generate_raw_cases(profile)
        assert raw == fabricated
        source = NUMBERED_PRD.read_text(encoding="utf-8")
        report = quality.check_grounding(raw, source, profile.frs)
        assert not report.ok, "LLM 编造内容应被溯源检查拦下"
        joined = "".join(i.suggestion for i in report.items)
        assert "30秒" in joined
        assert "限流降级" in joined or "熔断" in joined

    def test_llm_judge_with_source_marks_llm_fabrication(self, monkeypatch, tmp_path, offline_env):
        """judge（LLM 模式）+ --source：编造规则被标记为推测项。"""
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "sk-test-key")
        fake_review = json.dumps(
            {
                "cases": [
                    {
                        "title": t,
                        "scores": {d: 4 for d in ("完整性", "方法论标注合理性", "优先级合理性", "可执行性")},
                        "comment": "整体合格",
                    }
                    for t in ("[NFR-1.3] 验证虚构规则",)
                ],
                "suggestions": ["用例「a」建议补充边界", "用例「b」建议拆分步骤", "用例「c」建议补前置"],
            },
            ensure_ascii=False,
        )

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": fake_review}}]}

        monkeypatch.setattr("casepilot.core.llm.httpx.post", lambda *a, **k: FakeResponse())
        rows = [
            _case(),
            _case(title="[NFR-1.3] 验证虚构规则", expected="连续错误 5 次后锁定领取 30 分钟；触发限流降级"),
        ]
        kept, grounding = run_grounding_check(rows, NUMBERED_PRD, strict=True)
        assert len(kept) == 1
        report = judge_cases(kept, mode="llm", grounding=grounding)
        text = report.format_report("cases.xlsx")
        assert "幻觉溯源校验：推测项" in text
        assert "严格模式" in text
