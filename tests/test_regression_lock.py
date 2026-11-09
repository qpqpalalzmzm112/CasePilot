"""迭代 8 回归集保护 + FR 对账硬门禁验收测试（全离线）。

三部分：
1. examples/prd_login_full.md 回归锁（UP-3）：mock 规则自然产出「已验证好用例模式」
   （解锁边界两侧相邻值 / 验证码位数反例 / T0+604800 时间戳边界 / 3 次错锁 60 秒），
   迭代减法误删即 pytest 红；断言按标题 / 预期要素匹配，用例由规则产出而非注入。
2. FR 对账硬门禁（UP-1）：文档有编号但抽取 0 → 抛中文异常（CLI exit 1）；
   缺 FR 补齐失败 → 生成失败；核对表为「PRD 条款 → 用例编号」映射且覆盖率 100%。
3. 零漂移回归（DoD）：原有 5 样例 + 新 fixture 的用例数与方法论 / 类型分布锁定，
   mock 确定性（同输入同输出）保持。
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from casepilot.adapters import prd as prd_adapter
from casepilot.cli import app
from casepilot.core import mock_engine, quality
from casepilot.core.generator import (
    format_fr_coverage_report,
    generate_from_file,
    parse_document,
    reconcile_fr_coverage,
)
from tests.conftest import (
    BOUNDARY_PRD,
    EXAMPLES_DIR,
    LOGIN_PRD,
    NUMBERED_PRD,
    PETSTORE_OPENAPI,
    SECURITY_PERF_PRD,
)

LOGIN_FULL_PRD = EXAMPLES_DIR / "prd_login_full.md"

runner = CliRunner()
ALL_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD, NUMBERED_PRD]

# 迭代 8/9 零漂移基线：5 样例 + 新 fixture 的用例数与方法论 / 类型分布硬编码锁定。
# 迭代 9 计数变化明细（仅移除模板水分条目 + 多阈值完整性补齐，好用例零损失）：
# - prd_numbered 33 → 31：删 NFR-1.1 / NFR-1.2 的「异常类验证（自动补齐）」模板条目 2 条
#   （「系统接受有效输入并处理成功」同义反复），改为具体化补齐各 1 条判定标准用例；
# - prd_security_perf 35 → 31：删 4 个性能 FR 的「异常类验证（自动补齐）」模板条目 4 条，
#   改为具体化补齐各 1 条「目标规模下满足判定标准」用例；
# - prd_login_full 27 → 31：多阈值完整性（UP-3）——FR-3 条款 6 个阈值全部产出边界用例，
#   新增「60 秒验证码有效期」「连续 3 次输错」2 个阈值的 4 条边界用例（新增覆盖，非删减）；
# - prd_login 27 / prd_boundary 25 / openapi_petstore 30：计数不变。
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
    "prd_security_perf.md": {
        # 迭代 9 F3 引文完整化：FR-auto-8 由边界用例正确覆盖，去掉 1 条重复补齐水分
        "count": 30,
        "methods": {"安全测试": 18, "性能测试": 7, "场景法": 3, "判定表": 2},
        "categories": {"功能": 12, "异常": 8, "边界": 10},
    },
    "prd_numbered.md": {
        "count": 31,
        "methods": {"等价类": 12, "边界值": 10, "场景法": 3, "判定表": 2, "安全测试": 2, "性能测试": 2},
        "categories": {"功能": 11, "异常": 8, "边界": 12},
    },
    "prd_login_full.md": {
        "count": 31,
        "methods": {"等价类": 12, "边界值": 14, "场景法": 3, "判定表": 2},
        "categories": {"功能": 9, "异常": 8, "边界": 14},
    },
}

# fixture 的原文编号（单级 + 多级混排，含加粗 / 括号上下文），逐字保留
FULL_IDS = ("FR-1.1", "FR-1.2", "FR-1.3", "FR-3", "FR-4.3", "NFR-1.2")


def _combined(result) -> str:
    """兼容 click 不同版本的输出捕获方式。"""
    out = result.output or ""
    try:
        out += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    return out


def _blob(cases) -> list[str]:
    return [f"{c.title}\n{c.steps}\n{c.expected}" for c in cases]


class TestLoginFullNumbering:
    """DoD 1：新 fixture 单级 + 多级编号全部抽取，标签逐字一致，零 FR-auto。"""

    def setup_method(self):
        self.profile = parse_document(LOGIN_FULL_PRD)

    def test_all_ids_extracted_verbatim(self):
        assert tuple(f.id for f in self.profile.frs) == FULL_IDS
        assert all(not f.auto for f in self.profile.frs)

    def test_texts_are_source_clause_text(self):
        source = LOGIN_FULL_PRD.read_text(encoding="utf-8")
        for fr in self.profile.frs:
            assert fr.text in source, f"{fr.id} 条款文本应逐字来自原文"

    def test_tags_verbatim(self):
        assert [f.tag for f in self.profile.frs] == [f"[{i}]" for i in FULL_IDS]

    def test_single_and_multi_level_mix(self):
        """FR-3 单级 + FR-4.3 / FR-1.x 多级混排全部抽取。"""
        ids = [f.id for f in self.profile.frs]
        assert "FR-3" in ids and "FR-4.3" in ids and "FR-1.3" in ids

    def test_zero_auto_in_generated_titles(self, offline_env):
        result = generate_from_file(LOGIN_FULL_PRD, mode="mock")
        for case in result.cases:
            assert "FR-auto" not in case.title


class TestLoginFullCoverageTable:
    """DoD 1：核对表 100% 且含「PRD 条款 → 用例编号」映射。"""

    def setup_method(self):
        self.profile = parse_document(LOGIN_FULL_PRD)
        self.result = generate_from_file(LOGIN_FULL_PRD, mode="mock")
        self.merged, self.report = reconcile_fr_coverage(self.profile, self.result.cases)

    def test_coverage_100_with_case_number_mapping(self):
        text = format_fr_coverage_report(self.report)
        assert "FR 覆盖率 100%（6/6）" in text
        assert "| 编号 | 原文摘要 | 覆盖用例编号 | 状态 |" in text
        for item in self.report.items:
            assert item.case_numbers, f"{item.fr_id} 应登记覆盖用例编号"
            assert re.search(rf"\| {re.escape(item.fr_id)} \| [^|]+ \| #\d+", text)

    def test_every_fr_covered_by_tagged_case(self):
        for fr in self.profile.frs:
            assert any(fr.tag in c.title for c in self.merged), fr.tag

    def test_report_written_to_output(self, tmp_path, monkeypatch, offline_env):
        monkeypatch.chdir(tmp_path)
        generate_from_file(LOGIN_FULL_PRD, mode="mock")
        report = tmp_path / "output" / "prd_login_full_fr_coverage.md"
        assert report.is_file()
        assert "覆盖用例编号" in report.read_text(encoding="utf-8")


class TestGoodCasePatterns:
    """UP-3：已验证好用例模式由 mock 规则自然产出（断言存在即可，非注入）。"""

    def setup_method(self):
        self.result = generate_from_file(LOGIN_FULL_PRD, mode="mock")
        self.blobs = _blob(self.result.cases)

    def _hits(self, *keys) -> list[int]:
        return [i for i, t in enumerate(self.blobs) if all(k in t for k in keys)]

    def test_unlock_boundary_adjacent_values(self):
        """29 分 59 秒 / 30 分钟（1800 秒）解锁边界两侧相邻值用例。"""
        hits = self._hits("29 分 59 秒", "30 分钟", "1800")
        assert hits, "应产出解锁边界两侧相邻值用例（29 分 59 秒 / 30 分钟（1800 秒））"
        case = self.result.cases[hits[0]]
        assert "1799" in case.expected and "1801" in case.expected, "两侧相邻值均应出现"

    def test_verification_code_length_counterexamples(self):
        """验证码 3 位 / 5 位格式反例（4 位边界两侧相邻值）。"""
        hits = self._hits("3位", "5位")
        assert hits, "应产出验证码 3 位 / 5 位格式反例"
        assert any("4位" in self.blobs[i] or "4 位" in self.blobs[i] for i in hits)

    def test_timestamp_boundary_case(self):
        """T0+604800 / T0+604801 时间戳边界（7 天有效期）。"""
        hits = self._hits("T0+604800", "604801")
        assert hits, "应产出 T0+604800 / T0+604801 时间戳边界用例"

    def test_lock_rule_matches_prd_clause(self):
        """连续 3 次错验证码 → 登录按钮锁 60 秒（与 PRD 条款一致）。"""
        hits = self._hits("3 次", "60 秒", "锁定")
        assert hits, "应产出「连续 3 次错验证码 → 锁 60 秒」用例"
        assert any("登录按钮" in self.blobs[i] for i in hits)

    def test_lock_rule_not_rewritten_to_permanent(self):
        """条款一致性：不得改写成「永久锁定」类。"""
        for case in self.result.cases:
            assert "永久" not in f"{case.title}{case.steps}{case.expected}", case.title

    def test_patterns_come_from_mock_rules_not_injection(self):
        """好用例模式由 mock_engine 规则直出（raw 产物即含，而非对账注入）。"""
        profile = parse_document(LOGIN_FULL_PRD)
        raw = mock_engine.generate(profile)
        blobs = [f"{c['title']}\n{c['steps']}\n{c['expected']}" for c in raw]
        assert any("29 分 59 秒" in t for t in blobs)
        assert any("3位" in t and "5位" in t for t in blobs)
        assert any("T0+604800" in t and "604801" in t for t in blobs)

    def test_fixture_output_fully_grounded_and_clean(self):
        """新 fixture 产物：0 模糊词、0 测量值预言、0 推测项（不得误伤）。"""
        profile = parse_document(LOGIN_FULL_PRD)
        source = LOGIN_FULL_PRD.read_text(encoding="utf-8")
        for case in self.result.cases:
            assert quality.find_fuzzy_words(case.expected) == [], case.title
        assert quality.check_measurement_gate(self.result.cases, source) == []
        report = quality.check_grounding(self.result.cases, source, profile.frs)
        assert report.ok, [(i.kind, i.fragment) for i in report.items]


class TestHardGate:
    """UP-1 硬门禁：文档有编号但抽取 0 → exit 1 中文报错；补齐失败 → 生成失败。"""

    NUMBERED_BUT_UNEXTRACTABLE = (
        "# 订单系统需求\n\n"
        "## 概述\n\n"
        "本模块覆盖下单主流程，编号详见 FR-3 与 FR-4.3 的需求清单。\n\n"
        "## 功能清单\n\n"
        "- 支持下单\n"
        "- 支持取消订单\n"
    )

    def test_detect_numbering_style_but_zero_extract_raises(self):
        with pytest.raises(ValueError) as exc:
            prd_adapter.parse_text(self.NUMBERED_BUT_UNEXTRACTABLE, source="x.md")
        message = str(exc.value)
        assert "检测到原文编号样式" in message
        assert "抽取 0 条" in message
        assert "禁止静默回退自动编号" in message

    def test_cli_gen_exits_1_with_chinese_error(self, tmp_path):
        bad = tmp_path / "bad.md"
        bad.write_text(self.NUMBERED_BUT_UNEXTRACTABLE, encoding="utf-8")
        result = runner.invoke(app, ["gen", "-i", str(bad)])
        assert result.exit_code == 1
        text = _combined(result)
        assert "错误：" in text
        assert "检测到原文编号样式" in text and "抽取 0 条" in text

    def test_no_numbering_style_still_allows_auto_fallback(self):
        profile = prd_adapter.parse_text("## 功能清单\n\n- 支持登录\n", source="a.md")
        assert [f.id for f in profile.frs] == ["FR-auto-1"]
        assert all(f.auto for f in profile.frs)

    def test_gap_fr_registered_uncovered_not_watered(self):
        """迭代 9 / UP-1：补不出具体用例的 FR → 不塞模板条目，登记未覆盖清单 + 具体设计建议；
        产物无「系统接受有效输入并处理成功」类同义反复条目（DoD 1 注水清零）。"""
        text = "# 构造需求文档\n\n## 功能清单\n\n" + "".join(
            f"- 支持功能点{i}\n" for i in range(1, 9)
        )
        profile = prd_adapter.parse_text(text, source="gap.md")
        merged, report = reconcile_fr_coverage(profile, [])
        # 覆盖率如实计算（0/8），禁用模板填塞
        assert report.summary_line().startswith("FR 覆盖率 0%（0/8）")
        assert report.filled_count == 0
        assert len(report.uncovered_items) == 8
        assert merged == [], "补不出具体用例时不得产出任何凑数条目"
        # 未覆盖清单含具体设计建议（指向条款要素缺失，禁止空话）
        for item in report.uncovered_items:
            assert item.suggestion.startswith("建议"), item.fr_id
            assert "支持功能点" in item.suggestion
            assert "状态码" in item.suggestion or "阈值" in item.suggestion or "提示" in item.suggestion
        # 同义反复模板清零：产物与状态均无「自动补齐」模板痕迹
        for item in report.items:
            assert "自动补齐" not in item.status
            assert "系统接受有效输入并处理成功" not in item.suggestion

    def test_unregistered_gap_fails_generation(self, monkeypatch):
        """零遗漏登记（迭代 9 / UP-1）：既无覆盖又登记不了未覆盖清单 → 抛中文异常（生成失败）。"""
        monkeypatch.setattr(
            "casepilot.core.generator._fr_backfill_cases", lambda fr, module: []
        )
        monkeypatch.setattr(
            "casepilot.core.generator.build_design_suggestion", lambda fr: ""
        )
        text = "# 构造需求文档\n\n## 功能清单\n\n- 支持功能点甲\n"
        profile = prd_adapter.parse_text(text, source="gap.md")
        with pytest.raises(ValueError) as exc:
            reconcile_fr_coverage(profile, [])
        message = str(exc.value)
        assert "零遗漏登记未满足" in message
        assert "生成失败" in message

    def test_cli_gen_exit_1_on_unregistered_gap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "casepilot.core.generator._fr_backfill_cases", lambda fr, module: []
        )
        monkeypatch.setattr(
            "casepilot.core.generator.build_design_suggestion", lambda fr: ""
        )
        bad = tmp_path / "gap.md"
        bad.write_text(
            "# 构造需求文档\n\n## 功能清单\n\n"
            + "".join(f"- 支持功能点{i}\n" for i in range(1, 9)),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["gen", "-i", str(bad)])
        assert result.exit_code == 1
        assert "零遗漏登记未满足" in _combined(result)

    def test_cli_prints_fr_summary(self, tmp_path, offline_env):
        result = runner.invoke(
            app, ["gen", "-i", str(LOGIN_FULL_PRD), "-o", str(tmp_path / "out.xlsx")]
        )
        assert result.exit_code == 0, _combined(result)
        assert "FR 对账摘要：FR 覆盖率 100%（6/6）" in _combined(result)


class TestZeroDrift:
    """DoD 4 / 7：5 样例 + 新 fixture 生成数量与方法论分布零漂移、mock 确定性。"""

    @pytest.mark.parametrize(
        "path",
        [LOGIN_FULL_PRD, LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD, NUMBERED_PRD],
        ids=lambda p: p.stem,
    )
    def test_distribution_unchanged(self, path, offline_env):
        expected = REGRESSION_BASELINE[path.name]
        result = generate_from_file(path, mode="mock")
        methods: dict[str, int] = {}
        categories: dict[str, int] = {}
        for c in result.cases:
            methods[c.method] = methods.get(c.method, 0) + 1
            categories[c.category] = categories.get(c.category, 0) + 1
        assert len(result.cases) == expected["count"], f"{path.name} 用例数漂移"
        assert methods == expected["methods"], f"{path.name} 方法论分布漂移"
        assert categories == expected["categories"], f"{path.name} 类型分布漂移"

    def test_mock_deterministic_same_input_same_output(self, offline_env):
        first = generate_from_file(LOGIN_FULL_PRD, mode="mock")
        second = generate_from_file(LOGIN_FULL_PRD, mode="mock")
        assert [c.model_dump() for c in first.cases] == [c.model_dump() for c in second.cases]
        profile = parse_document(LOGIN_FULL_PRD)
        text1 = format_fr_coverage_report(reconcile_fr_coverage(profile, first.cases)[1])
        text2 = format_fr_coverage_report(reconcile_fr_coverage(profile, second.cases)[1])
        assert text1 == text2
