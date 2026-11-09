"""UP-1 验收测试：原文编号 FR/NFR 清单逐字引用 + NFR 覆盖入表 + 缺漏补齐 + 样例回归（全离线）。

验收标准：
- examples/prd_numbered.md（FR-1.x / FR-2.x / NFR-1.x 原文编号）生成后，
  标题标签与原文编号 100% 逐字一致（逐条比对断言）；
- NFR 条目出现在覆盖率核对表且每条 ≥1 用例；
- 缺漏补齐路径有单测（含 NFR 缺漏）；
- 原有 4 样例回归不回退（无编号文档走自动编号兜底并在报告注明）。
"""

from __future__ import annotations

import re

import pytest

from casepilot.core import mock_engine
from casepilot.core.generator import (
    format_fr_coverage_report,
    generate_from_file,
    parse_document,
    reconcile_fr_coverage,
)
from casepilot.core.schema import TestCase, validate_cases
from tests.conftest import (
    BOUNDARY_PRD,
    LOGIN_PRD,
    NUMBERED_PRD,
    PETSTORE_OPENAPI,
    SECURITY_PERF_PRD,
)

# 编号样例的原文编号（PRD 内逐字出现，顺序即文档顺序）
ORIGINAL_IDS = (
    "FR-1.1", "FR-1.2", "FR-1.3", "FR-2.1", "FR-2.2", "FR-2.3",
    "NFR-1.1", "NFR-1.2", "NFR-1.3",
)

# 迭代 7 前的基线（用例数 + 方法论 / 类型分布）：回归断言硬编码，保证零漂移
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
        # 迭代 9 F3 引文完整化：FR-auto-8「支持 50 并发用户同时登录」由边界用例
        # 正确覆盖（引用含完整条款原文），去掉 1 条与边界用例重叠的重复补齐
        # （性能/功能「目标规模判定标准」水分），好用例零损失
        "count": 30,
        "methods": {"安全测试": 18, "性能测试": 7, "场景法": 3, "判定表": 2},
        "categories": {"功能": 12, "异常": 8, "边界": 10},
    },
}

ALL_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD]
PRD_SAMPLES = [LOGIN_PRD, BOUNDARY_PRD, SECURITY_PERF_PRD]


class TestNumberedExtraction:
    """原文编号抽取：FR / NFR 分层编号逐字保留，记录原文条款文本。"""

    def setup_method(self):
        self.profile = parse_document(NUMBERED_PRD)

    def test_ids_verbatim_in_document_order(self):
        assert tuple(f.id for f in self.profile.frs) == ORIGINAL_IDS

    def test_texts_are_source_clause_text(self):
        """每条 FR/NFR 记录原文条款文本（编号后到行尾）。"""
        source = NUMBERED_PRD.read_text(encoding="utf-8")
        for fr in self.profile.frs:
            assert fr.text in source, f"{fr.id} 条款文本应逐字来自原文"
            assert fr.auto is False

    def test_tags_verbatim(self):
        """追溯前缀 [FR-1.1] / [NFR-1.2] 与原文编号逐字一致。"""
        assert [fr.tag for fr in self.profile.frs] == [f"[{i}]" for i in ORIGINAL_IDS]

    def test_nfr_and_fr_sections_recorded(self):
        sections = {fr.id: fr.section for fr in self.profile.frs}
        assert sections["FR-1.1"] == "促销活动"
        assert sections["FR-2.1"] == "订单核销"
        assert sections["NFR-1.1"] == "非功能需求"


class TestVerbatimTitleTags:
    """验收：生成后标题标签与原文编号 100% 逐字一致（逐条比对）。"""

    def setup_method(self):
        self.profile = parse_document(NUMBERED_PRD)
        self.result = generate_from_file(NUMBERED_PRD, mode="mock")
        self.source = NUMBERED_PRD.read_text(encoding="utf-8")

    def test_every_original_id_covered_by_tagged_case(self):
        for fr in self.profile.frs:
            hits = [c.title for c in self.result.cases if fr.tag in c.title]
            assert hits, f"{fr.tag}（{fr.text}）应至少有 1 条带追溯前缀的用例"

    def test_all_title_tags_verbatim_match_original_ids(self):
        """逐条比对：标题中所有 [编号] 标签与原文编号 100% 逐字一致。"""
        tags: list[str] = []
        for case in self.result.cases:
            tags.extend(re.findall(r"\[([^\[\]]+)\]", case.title))
        assert tags, "应存在带 [编号] 追溯前缀的用例"
        for tag in tags:
            assert tag in ORIGINAL_IDS, f"标题标签「{tag}」不是 PRD 原文编号"
            assert f"[{tag}]" in self.source or tag in self.source, f"「{tag}」须逐字出现在 PRD 原文"
        assert set(tags) == set(ORIGINAL_IDS), "原文编号应 100% 被标题标签覆盖"

    def test_no_auto_numbering_for_numbered_doc(self):
        assert all(not fr.auto for fr in self.profile.frs)
        for case in self.result.cases:
            assert "FR-auto" not in case.title

    def test_all_cases_valid_testcase(self):
        for case in self.result.cases:
            assert isinstance(case, TestCase)


class TestNFRInCoverageTable:
    """验收：NFR 条目出现在覆盖率核对表且被覆盖（每条 ≥1 用例）。"""

    def setup_method(self):
        self.profile = parse_document(NUMBERED_PRD)
        self.result = generate_from_file(NUMBERED_PRD, mode="mock")
        self.merged, self.report = reconcile_fr_coverage(self.profile, self.result.cases)

    def test_table_rows_for_every_original_id(self):
        text = format_fr_coverage_report(self.report)
        for fr in self.profile.frs:
            assert f"| {fr.id} |" in text, f"覆盖率核对表缺少 {fr.id} 行"

    def test_nfr_rows_have_at_least_one_case(self):
        rows = {item.fr_id: item for item in self.report.items}
        for nfr_id in ("NFR-1.1", "NFR-1.2", "NFR-1.3"):
            assert nfr_id in rows
            assert rows[nfr_id].count >= 1
            fr = next(f for f in self.profile.frs if f.id == nfr_id)
            assert any(fr.tag in c.title for c in self.merged)

    def test_every_row_status_marked(self):
        text = format_fr_coverage_report(self.report)
        for item in self.report.items:
            assert ("已补齐" if item.filled else "✓") in text

    def test_report_written_to_output(self, tmp_path, monkeypatch, offline_env):
        monkeypatch.chdir(tmp_path)
        generate_from_file(NUMBERED_PRD, mode="mock")
        report = tmp_path / "output" / "prd_numbered_fr_coverage.md"
        assert report.is_file()
        text = report.read_text(encoding="utf-8")
        assert "编号逐字引用 PRD 原文编号" in text
        assert "| NFR-1.3 |" in text


class TestNFRBackfill:
    """缺漏补齐路径单测（含 NFR 缺漏，迭代 9 具体化补齐）：条款有要素才补齐，补不出登记未覆盖。"""

    def test_nfr_gap_concrete_filled_with_specialized_method(self):
        profile = parse_document(NUMBERED_PRD)
        # 只喂 1 条无关用例：全部 FR/NFR 缺漏，走具体化补齐 / 未覆盖登记路径
        unrelated = validate_cases(
            [
                {
                    "title": "与需求无关的用例",
                    "precondition": "系统可用",
                    "steps": "1. 执行操作\n2. 查看结果",
                    "expected": "结果正确",
                    "priority": "P1",
                    "category": "功能",
                    "method": "等价类",
                    "module": "m/x",
                }
            ]
        )
        merged, report = reconcile_fr_coverage(profile, unrelated)
        # FR-2.1（无任何可验证要素）→ 登记未覆盖，不凑数；其余 8 条具体化补齐
        assert report.filled_count == 8
        assert [item.fr_id for item in report.uncovered_items] == ["FR-2.1"]
        assert "优惠券" in report.uncovered_items[0].suggestion
        for fr in profile.frs:
            if fr.id == "FR-2.1":
                continue
            backfilled = [c for c in merged if c.title.startswith(fr.tag)]
            assert backfilled, f"{fr.tag} 含具体要素，应具体化补齐"
            assert all("（具体化补齐）" in c.title for c in backfilled)
        # NFR-1.1（响应时间）→ 性能测试、NFR-1.3（锁定）→ 安全测试
        by_tag = {fr.id: [c for c in merged if c.title.startswith(fr.tag)] for fr in profile.frs}
        assert all(c.method == "性能测试" for c in by_tag["NFR-1.1"])
        assert all(c.method == "安全测试" for c in by_tag["NFR-1.3"])
        # 触发类条款（NFR-1.3）产阈值边界 + 相邻反例 2 条，判定标准类（NFR-1.1）产 1 条
        assert len(by_tag["NFR-1.3"]) == 2
        assert len(by_tag["NFR-1.1"]) == 1
        text = format_fr_coverage_report(report)
        assert "已补齐" in text
        assert "⚠ 未覆盖（已列清单）" in text

    def test_backfill_cases_keep_verbatim_tags(self):
        """具体化补齐用例标题保留原文编号追溯前缀；补不出的 FR 零遗漏登记进未覆盖清单。"""
        profile = parse_document(NUMBERED_PRD)
        merged, report = reconcile_fr_coverage(profile, [])
        uncovered_ids = {item.fr_id for item in report.uncovered_items}
        for fr in profile.frs:
            if fr.id in uncovered_ids:
                assert not any(c.title.startswith(fr.tag) for c in merged), fr.id
            else:
                assert any(c.title.startswith(fr.tag) for c in merged), fr.tag


class TestRegressionFourSamples:
    """原有 4 样例回归：用例数与方法论 / 类型分布与迭代 7 前完全一致。"""

    @pytest.mark.parametrize("path", ALL_SAMPLES, ids=lambda p: p.stem)
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

    @pytest.mark.parametrize("path", PRD_SAMPLES, ids=lambda p: p.stem)
    def test_unnumbered_samples_use_auto_numbering_with_note(self, path, offline_env):
        """无编号文档走自动编号兜底（FR-auto-1…），对账报告注明「自动编号」。"""
        result = generate_from_file(path, mode="mock")
        profile = parse_document(path)
        assert all(fr.auto for fr in profile.frs)
        _, report = reconcile_fr_coverage(profile, result.cases)
        text = format_fr_coverage_report(report)
        assert "自动编号" in text and "FR-auto-1" in text

    def test_openapi_keeps_no_fr_reconciliation(self, offline_env):
        """OpenAPI 输入不做 FR 对账（api 命令行为不变）。"""
        result = generate_from_file(PETSTORE_OPENAPI, mode="mock")
        profile = parse_document(PETSTORE_OPENAPI)
        assert profile.frs == []
        _, report = reconcile_fr_coverage(profile, result.cases)
        assert report is None
