"""golden 基准集完整性测试：3 个样例集的数量、维度覆盖与人工质量底线。"""

from __future__ import annotations

import re

import pytest

from casepilot.core.schema import CATEGORIES, PRIORITIES
from evals.metrics import DATASETS_DIR, load_golden

DATASET_FILES = (
    "prd_login_golden.json",
    "openapi_petstore_golden.json",
    "prd_boundary_golden.json",
)


@pytest.mark.parametrize("name", DATASET_FILES)
class TestGoldenDatasets:
    def test_at_least_15_cases(self, name):
        golden = load_golden(DATASETS_DIR / name)
        assert len(golden) >= 15

    def test_method_coverage_at_least_3(self, name):
        golden = load_golden(DATASETS_DIR / name)
        methods = {g.case.method for g in golden}
        assert len(methods) >= 3

    def test_category_coverage_at_least_3(self, name):
        golden = load_golden(DATASETS_DIR / name)
        categories = {g.case.category for g in golden}
        assert len(categories) >= 3
        assert categories <= set(CATEGORIES)

    def test_priority_p0_to_p2_present(self, name):
        golden = load_golden(DATASETS_DIR / name)
        priorities = {g.case.priority for g in golden}
        assert {"P0", "P1", "P2"} <= priorities
        assert priorities <= set(PRIORITIES)

    def test_every_case_has_design_note(self, name):
        golden = load_golden(DATASETS_DIR / name)
        assert all(g.note.strip() for g in golden), "golden 必须带人工设计理由 note"

    def test_expected_has_acceptance_criteria(self, name):
        """人工质量底线：预期结果含判定标准（具体数值 / 提示语 / 状态变化动词）。"""
        golden = load_golden(DATASETS_DIR / name)
        criteria_re = re.compile(
            r"\d|「|」|成功|失败|跳转|提示|拒绝|返回|展示|锁定|取消|失效|通过|回滚|一致|生效|移除|覆盖|清除"
        )
        for g in golden:
            assert criteria_re.search(g.case.expected), f"预期结果缺少判定标准：{g.case.title}"

    def test_steps_are_concrete(self, name):
        """人工质量底线：步骤为多步编号步骤，不是一句话套话。"""
        golden = load_golden(DATASETS_DIR / name)
        for g in golden:
            assert len(re.findall(r"^\s*\d+[.、)]", g.case.steps, flags=re.MULTILINE)) >= 2, (
                f"步骤应至少 2 个编号步骤：{g.case.title}"
            )
