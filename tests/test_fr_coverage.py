"""UP-1 需求对账机制测试：FR 清单提取 + 逐条覆盖核对 + 缺漏自动补齐 + 对账报告（全离线）。"""

from __future__ import annotations

import logging
import re

import pytest

from casepilot.adapters import prd
from casepilot.core import mock_engine
from casepilot.core.generator import (
    format_fr_coverage_report,
    generate_from_file,
    parse_document,
    reconcile_fr_coverage,
)
from casepilot.core.schema import TestCase, validate_cases
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, NUMBERED_PRD, PETSTORE_OPENAPI

# 人为构造输入：8 个功能点，规则引擎只覆盖前 MAX_FEATURES 个，后 2 个 FR 必须被自动补齐
GAP_PRD = (
    "# 构造需求文档\n\n"
    "## 功能清单\n\n"
    + "".join(f"- 支持功能点{i}\n" for i in range(1, 9))
)


class TestFRExtraction:
    """FR 清单提取：显式编号优先，无编号按功能点自动编号。"""

    def test_explicit_prefix_forms(self):
        text = "# 需求\n\n## 需求清单\n\nFR-1.1 支持账号密码登录\n需求3 支持导出报表\nR2 支持注册\n"
        profile = prd.parse_text(text, source="x.md")
        # 迭代 7：编号逐字保留原文（大小写、分隔符不改写）
        assert [(f.id, f.text) for f in profile.frs] == [
            ("FR-1.1", "支持账号密码登录"),
            ("需求3", "支持导出报表"),
            ("R2", "支持注册"),
        ]

    def test_nfr_prefix_forms(self):
        """FR / NFR 分层编号逐字保留（含中英文冒号 / 顿号后缀）。"""
        text = (
            "# 需求\n\n"
            "NFR-1.2：支持 50 并发用户同时登录\n"
            "NFR-2.1、接口响应时间不超过 2 秒\n"
            "fr-3.1：小写编号保持原样\n"
        )
        profile = prd.parse_text(text, source="x.md")
        assert [f.id for f in profile.frs] == ["NFR-1.2", "NFR-2.1", "fr-3.1"]

    def test_explicit_hierarchical_number(self):
        profile = prd.parse_text("# 需求\n\n1.4 支持导入数据\n", source="x.md")
        assert [(f.id, f.text) for f in profile.frs] == [("1.4", "支持导入数据")]

    def test_flat_numbered_steps_are_not_fr(self):
        """一级编号「1. 步骤」是流程步骤，不是显式 FR 编号。"""
        profile = prd.parse_text("## 主流程\n\n1. 打开登录页面\n2. 点击登录\n", source="x.md")
        assert profile.frs == []
        assert profile.flows == ["打开登录页面", "点击登录"]

    def test_explicit_fr_also_registered_as_feature(self):
        """显式 FR 同步登记功能点，规则引擎可为其生成用例。"""
        profile = prd.parse_text("# 需求\n\nFR-1.1 支持账号密码登录\n", source="x.md")
        assert [f.name for f in profile.features] == ["支持账号密码登录"]

    def test_auto_numbering_from_features(self):
        profile = prd.parse_text("## 功能清单\n\n- 支持登录\n- 支持注册\n", source="a.md")
        # 迭代 7：无任何原文编号时才自动编号兜底（FR-auto-1…，auto=True）
        assert [(f.id, f.text, f.auto) for f in profile.frs] == [
            ("FR-auto-1", "支持登录", True),
            ("FR-auto-2", "支持注册", True),
        ]

    def test_explicit_takes_priority_over_auto(self):
        text = "# 需求\n\n## 功能清单\n\nFR-2.1 支持登录\n- 支持注册\n"
        profile = prd.parse_text(text, source="a.md")
        # 有显式编号时不做自动编号（优先识别显式编号）
        assert [f.id for f in profile.frs] == ["FR-2.1"]
        assert [f.auto for f in profile.frs] == [False]

    def test_login_sample_fr_matches_features(self):
        profile = parse_document(LOGIN_PRD)
        assert len(profile.frs) == len(profile.features) == 6
        assert [f.text for f in profile.frs] == [f.name for f in profile.features]

    def test_boundary_sample_fr_count(self):
        profile = parse_document(BOUNDARY_PRD)
        assert len(profile.frs) == 5

    def test_openapi_has_no_fr(self):
        """OpenAPI 输入不提取 FR 清单，api 命令行为不变。"""
        profile = parse_document(PETSTORE_OPENAPI)
        assert profile.frs == []


class TestExtractionRobustness:
    """迭代 8 / UP-1 抽取鲁棒化：单级 / 多级 / 加粗 / 括号 / 表格 / 全角连字符。"""

    def test_single_level_number(self):
        """FR-3 单级编号（历史漏抽根因）必须抽取。"""
        profile = prd.parse_text("# 需求\n\nFR-3：支持忘记密码找回\n", source="x.md")
        assert [(f.id, f.text) for f in profile.frs] == [("FR-3", "支持忘记密码找回")]

    def test_multi_level_number(self):
        profile = prd.parse_text(
            "# 需求\n\nFR-1.3.2：支持多级编号\nFR-4.3：设备挤下线\n", source="x.md"
        )
        assert [f.id for f in profile.frs] == ["FR-1.3.2", "FR-4.3"]

    def test_bold_context(self):
        profile = prd.parse_text("# 需求\n\n**FR-3**：支持忘记密码找回\n", source="x.md")
        assert [f.id for f in profile.frs] == ["FR-3"]

    def test_paren_context(self):
        profile = prd.parse_text("# 需求\n\n（FR-3）支持忘记密码找回\n", source="x.md")
        assert [(f.id, f.text) for f in profile.frs] == [("FR-3", "支持忘记密码找回")]

    def test_full_width_dash_kept_verbatim(self):
        """全角连字符「FR—3」容忍且逐字保留。"""
        profile = prd.parse_text("# 需求\n\nFR—3：全角连字符编号\n", source="x.md")
        assert [f.id for f in profile.frs] == ["FR—3"]

    def test_table_cell_numbering(self):
        """表格单元格内编号（| FR-3 | 支持忘记密码 |）。"""
        text = "# 需求\n\n| 编号 | 描述 |\n| --- | --- |\n| FR-3 | 支持忘记密码 |\n"
        profile = prd.parse_text(text, source="x.md")
        assert [(f.id, f.text) for f in profile.frs] == [("FR-3", "支持忘记密码")]

    def test_hierarchical_number_with_decorations(self):
        profile = prd.parse_text("# 需求\n\n**1.4**：支持导入数据\n", source="x.md")
        assert [f.id for f in profile.frs] == ["1.4"]

    def test_numbering_style_count_tolerates_full_width_dash(self):
        assert prd._count_numbering_styles("详见 FR—3 与 NFR—1.2 的说明") == 2

    def test_prose_requirement_word_not_counted(self):
        """正文「用户需求 3 天完成」不计入编号样式（防误伤）。"""
        assert prd._count_numbering_styles("用户需求 3 天内完成响应") == 0


class TestReconciliation:
    """生成后 FR 逐条对账：覆盖核对、追溯前缀、缺漏自动补齐。"""

    @pytest.mark.parametrize("path", [LOGIN_PRD, BOUNDARY_PRD])
    def test_samples_full_coverage_with_tag_prefix(self, path, offline_env):
        """验收：login / boundary 样例 FR 覆盖率 100%，每 FR ≥1 条用例标题含 [FR-x.y]。"""
        result = generate_from_file(path, mode="mock")
        profile = parse_document(path)
        for fr in profile.frs:
            tagged = [c for c in result.cases if fr.tag in c.title]
            assert tagged, f"{fr.tag}（{fr.text}）应至少有 1 条带追溯前缀的用例"
        _, report = reconcile_fr_coverage(profile, result.cases)
        assert report is not None
        assert report.filled_count == 0, "两个样例的 FR 均应被规则生成用例命中，无需补齐"

    def test_gap_fr_registered_uncovered_without_filler(self):
        """迭代 9 / UP-1：部分 FR 补不出具体用例时登记未覆盖清单（含具体设计建议），
        不再模板填塞「系统接受有效输入并处理成功」类条目。"""
        profile = prd.parse_text(GAP_PRD, source="gap.md")
        raw = mock_engine.generate(profile)
        cases = validate_cases(raw)
        assert len(profile.frs) == 8

        merged, report = reconcile_fr_coverage(profile, cases)
        assert report is not None
        assert report.filled_count == 0, "禁用模板填塞：补不出具体用例不得凑数补齐"
        assert report.added_cases == 0
        assert len(merged) == len(cases), "用例集不得因凑数覆盖而膨胀"

        uncovered = report.uncovered_items
        assert [item.fr_id for item in uncovered] == ["FR-auto-7", "FR-auto-8"]
        for item in uncovered:
            assert item.suggestion.startswith("建议"), item.fr_id
            assert "支持功能点" in item.suggestion, "设计建议须引用条款要素，禁止空话"
        text = format_fr_coverage_report(report)
        assert "未覆盖清单" in text
        assert "⚠ 未覆盖（已列清单）" in text
        # 无任何「已补齐」条目行（图例说明除外），亦无同义反复模板
        assert "✓ 已补齐（具体化补齐） |" not in text
        assert "系统接受有效输入并处理成功" not in text

    def test_concrete_fill_when_clause_has_elements(self):
        """条款含具体要素（数值 / 提示文案）时具体化补齐：预期含具体验证点，标记「已补齐」。"""
        text = (
            "# 需求\n\n## 功能清单\n\n"
            + "".join(f"- 支持功能点{i}\n" for i in range(1, 8))
            + "\n## 安全要求\n\n- 防 SQL 注入拦截，命中时提示「请求非法」\n"
        )
        profile = prd.parse_text(text, source="gap3.md")
        merged, report = reconcile_fr_coverage(profile, [])
        security_frs = [f for f in profile.frs if "注入" in f.text]
        assert security_frs, "构造输入应含安全类 FR"
        for fr in security_frs:
            filled = [c for c in merged if c.title.startswith(fr.tag)]
            assert filled, f"{fr.tag} 含提示文案要素，应具体化补齐"
            assert all("（具体化补齐）" in c.title for c in filled)
            assert all(c.method == "安全测试" for c in filled)
            # 具体验证点：提示文案逐字引用
            assert all("「请求非法」" in c.expected for c in filled)
        assert report.filled_count == 1
        assert any(item.suggestion for item in report.items), "要素缺失的 FR 登记未覆盖"

    def test_backfill_method_follows_fr_content(self):
        """缺漏 FR 的具体化补齐按 FR 内容选方法论：安全 / 性能词 → 安全测试 / 性能测试。"""
        text = (
            "# 需求\n\n## 功能清单\n\n"
            + "".join(f"- 支持功能点{i}\n" for i in range(1, 9))
            + "\n## 安全要求\n\n- 防 SQL 注入拦截，命中时提示「请求非法」\n"
        )
        profile = prd.parse_text(text, source="gap2.md")
        cases = validate_cases(mock_engine.generate(profile))
        merged, report = reconcile_fr_coverage(profile, cases)
        security_frs = [f for f in profile.frs if "注入" in f.text]
        assert security_frs, "构造输入应含安全类 FR"
        for fr in security_frs:
            backfilled = [c for c in merged if c.title.startswith(fr.tag) and "具体化补齐" in c.title]
            assert backfilled, f"{fr.tag} 应被具体化补齐"
            assert all(c.method == "安全测试" for c in backfilled)

    def test_report_markdown_format(self):
        profile = prd.parse_text(GAP_PRD, source="gap.md")
        cases = validate_cases(mock_engine.generate(profile))
        _, report = reconcile_fr_coverage(profile, cases)
        text = format_fr_coverage_report(report)
        # 迭代 8/9：核对表为「PRD 条款 → 用例编号」映射 + 三态 + 未覆盖清单
        assert "| 编号 | 原文摘要 | 覆盖用例编号 | 状态 |" in text
        assert "| FR-auto-1 | 支持功能点1 |" in text
        assert "FR 覆盖率" in text
        assert "✓ 质量覆盖" in text
        assert "⚠ 未覆盖（已列清单）" in text
        assert "## 未覆盖清单（含具体设计建议）" in text

    def test_report_notes_auto_numbering_fallback(self):
        """无编号文档的对账报告必须注明「自动编号」兜底（迭代 7 / UP-1）。"""
        profile = prd.parse_text(GAP_PRD, source="gap.md")
        cases = validate_cases(mock_engine.generate(profile))
        _, report = reconcile_fr_coverage(profile, cases)
        text = format_fr_coverage_report(report)
        assert "自动编号" in text
        assert "FR-auto-1" in text

    def test_report_notes_original_numbering_for_numbered_doc(self):
        """带原文编号的文档报告注明编号逐字引用原文，不写「自动编号」。"""
        profile = parse_document(NUMBERED_PRD)
        cases = validate_cases(mock_engine.generate(profile))
        _, report = reconcile_fr_coverage(profile, cases)
        text = format_fr_coverage_report(report)
        assert "编号逐字引用 PRD 原文编号" in text
        assert "自动编号" not in text

    def test_report_deterministic(self):
        profile = prd.parse_text(GAP_PRD, source="gap.md")
        cases = validate_cases(mock_engine.generate(profile))
        first = format_fr_coverage_report(reconcile_fr_coverage(profile, cases)[1])
        second = format_fr_coverage_report(reconcile_fr_coverage(profile, cases)[1])
        assert first == second

    def test_openapi_reconciliation_untouched(self):
        """OpenAPI 输入不做 FR 对账：返回原用例集、报告为 None（api 命令行为不变）。"""
        profile = parse_document(PETSTORE_OPENAPI)
        cases = validate_cases(mock_engine.generate(profile))
        merged, report = reconcile_fr_coverage(profile, cases)
        assert report is None
        assert [c.model_dump() for c in merged] == [c.model_dump() for c in cases]


class TestReportFile:
    def test_report_written_to_output(self, tmp_path, monkeypatch, offline_env):
        """验收：对账报告落盘 output/<输入名>_fr_coverage.md。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("casepilot.core.generator.FR_REPORT_DIR", "output")
        generate_from_file(LOGIN_PRD, mode="mock")
        report = tmp_path / "output" / "prd_login_fr_coverage.md"
        assert report.is_file()
        text = report.read_text(encoding="utf-8")
        assert "FR 需求对账报告" in text
        assert "FR 覆盖率 100%（6/6）" in text

    def test_no_report_for_openapi(self, tmp_path, monkeypatch, offline_env):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("casepilot.core.generator.FR_REPORT_DIR", "output")
        generate_from_file(PETSTORE_OPENAPI, mode="mock")
        assert not (tmp_path / "output" / "openapi_petstore_fr_coverage.md").is_file()

    def test_fr_report_printed_via_logger(self, offline_env, caplog):
        with caplog.at_level(logging.INFO, logger="casepilot"):
            generate_from_file(BOUNDARY_PRD, mode="mock")
        assert "FR 需求对账报告" in caplog.text
        assert "FR 覆盖率 100%（5/5）" in caplog.text


class TestDesignSuggestionAndTimepointFill:
    """迭代 9 / UP-1：要素化设计建议（阈值 / 时间点 / 提示文案）+ 时间点条款具体化补齐。"""

    ELEMENTS_PRD = (
        "# 需求\n\n## 提醒规则\n\n"
        "FR-1：单笔超 10000 或当日累计 50000 触发短信提醒\n"
        "FR-2：07:00/22:00 电价切换\n"
        "FR-3：弹窗提示「操作成功」\n"
    )

    def test_suggestion_derives_from_clause_elements(self, monkeypatch):
        """未覆盖清单的建议从条款要素推导：阈值两侧相邻值 / 时刻前后 / 提示文案逐字。"""
        monkeypatch.setattr(
            "casepilot.core.generator._fr_backfill_cases", lambda fr, module: []
        )
        profile = prd.parse_text(self.ELEMENTS_PRD, source="s.md")
        merged, report = reconcile_fr_coverage(profile, [])
        assert merged == []
        by_id = {item.fr_id: item.suggestion for item in report.items}
        assert "阈值 10000" in by_id["FR-1"] and "9999 不触发" in by_id["FR-1"]
        assert "阈值 50000" in by_id["FR-1"] and "49999 不触发" in by_id["FR-1"]
        assert "切换时刻前（06:59）/时刻整（07:00）/时刻后（07:01）" in by_id["FR-2"]
        assert "提示「操作成功」逐字一致" in by_id["FR-3"]

    def test_timepoint_clause_fill(self):
        """时间点条款具体化补齐：切换时刻前 / 整 / 后三时点对照用例。"""
        profile = prd.parse_text(self.ELEMENTS_PRD, source="s.md")
        merged, report = reconcile_fr_coverage(profile, [])
        filled = [c for c in merged if c.title.startswith("[FR-2]")]
        assert filled, "时间点条款应具体化补齐"
        joined = "\n".join(f"{c.title}\n{c.expected}" for c in filled)
        assert "06:59" in joined and "07:00" in joined and "07:01" in joined
        assert "21:59" in joined and "22:00" in joined and "22:01" in joined

    def test_prompt_only_clause_fill_cites_prompt(self):
        """仅含提示文案的条款产 1 条判定标准用例，提示逐字引用。"""
        profile = prd.parse_text(self.ELEMENTS_PRD, source="s.md")
        merged, _ = reconcile_fr_coverage(profile, [])
        filled = [c for c in merged if c.title.startswith("[FR-3]")]
        assert len(filled) == 1
        assert "「操作成功」" in filled[0].expected
        assert filled[0].title.endswith("（具体化补齐）")

    def test_fill_summary_no_ellipsis(self):
        """修 bug：标题摘要取条款核心短语，不带省略号「…」。"""
        from casepilot.core.generator import _fr_summary

        assert "…" not in _fr_summary("支持忘记密码找回，通过短信验证码验证身份后重置密码，整块需求覆盖")
        assert _fr_summary("支持忘记密码找回，通过短信验证码验证身份后重置密码") == "支持忘记密码找回"

    def test_fill_module_not_document_name(self):
        """修 bug：补齐用例模块取「父模块/子模块」真实路径，不把文档名当模块。"""
        profile = prd.parse_text(
            "# 优惠券系统需求规格说明书\n\n## 非功能需求\n\nNFR-1：优惠券核销接口响应时间 ≤2 秒\n",
            source="m.md",
        )
        merged, _ = reconcile_fr_coverage(profile, [])
        filled = [c for c in merged if c.title.startswith("[NFR-1]")]
        assert filled
        for c in filled:
            assert c.module == "非功能需求/性能验证", c.module
            assert "需求规格说明书" not in c.module


class TestTitlePrefix:
    def test_covered_case_title_carries_tag(self, offline_env):
        """已覆盖 FR 的首条覆盖用例补 [FR-auto-x] 追溯前缀，其余用例不重复打标。"""
        result = generate_from_file(LOGIN_PRD, mode="mock")
        tagged = [c for c in result.cases if c.title.startswith("[FR-")]
        assert len(tagged) == 6
        for c in tagged:
            assert re.fullmatch(r"\[FR-auto-\d+\] .+", c.title)

    def test_tagged_case_is_valid_testcase(self, offline_env):
        result = generate_from_file(LOGIN_PRD, mode="mock")
        for c in result.cases:
            assert isinstance(c, TestCase)
