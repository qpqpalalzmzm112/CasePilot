"""mock 确定性规则引擎测试：确定性、方法论覆盖与优先级规则（UP-2 / UP-3）。"""

import pytest

from casepilot.core import mock_engine
from casepilot.core.schema import (
    CATEGORIES,
    METHODS,
    Boundary,
    DocumentProfile,
    Feature,
    TestCase,
)


def _full_profile() -> DocumentProfile:
    """包含功能点、边界与流程的完整画像。"""
    return DocumentProfile(
        source="prd_login.md",
        source_type="prd",
        module="登录模块",
        features=[
            Feature(name="支持账号密码登录"),
            Feature(name="POST /pet 创建宠物", kind="api"),
        ],
        boundaries=[
            Boundary(text="密码长度 8-20 位", value=8, kind="range_min", unit="位"),
            Boundary(text="密码长度 8-20 位", value=20, kind="range_max", unit="位"),
            Boundary(text="连续错误 5 次锁定", value=5, kind="threshold", unit="次"),
        ],
        flows=["打开登录页", "输入账号密码", "点击登录"],
    )


class TestDeterminism:
    def test_same_input_same_output(self):
        profile = _full_profile()
        first = mock_engine.generate(profile)
        second = mock_engine.generate(profile)
        assert first == second

    def test_output_validates_as_testcases(self):
        for raw in mock_engine.generate(_full_profile()):
            case = TestCase.model_validate(raw)  # 不应抛异常
            assert case.method in METHODS


class TestCoverageRules:
    def test_min_case_count(self):
        assert len(mock_engine.generate(_full_profile())) >= mock_engine.MIN_CASES

    def test_method_coverage_at_least_three(self):
        methods = {c["method"] for c in mock_engine.generate(_full_profile())}
        assert len(methods) >= mock_engine.MIN_METHOD_COVERAGE
        assert methods <= set(METHODS)

    def test_category_coverage_at_least_three(self):
        categories = {c["category"] for c in mock_engine.generate(_full_profile())}
        assert len(categories) >= mock_engine.MIN_CATEGORY_COVERAGE
        assert categories <= set(CATEGORIES)

    def test_every_case_method_not_empty(self):
        for c in mock_engine.generate(_full_profile()):
            assert c["method"] in METHODS
            assert c["category"] in CATEGORIES


class TestPriorityRules:
    def test_main_flow_is_p0(self):
        cases = mock_engine.generate(_full_profile())
        p0_cases = [c for c in cases if c["priority"] == "P0"]
        assert p0_cases, "主流程基本流用例应为 P0"
        assert all(c["method"] == "场景法" for c in p0_cases)

    def test_valid_equivalence_is_p1(self):
        cases = mock_engine.generate(_full_profile())
        valid = [c for c in cases if "有效等价类" in c["title"]]
        assert valid and all(c["priority"] == "P1" for c in valid)

    def test_invalid_class_is_p2(self):
        cases = mock_engine.generate(_full_profile())
        invalid = [c for c in cases if "无效等价类" in c["title"]]
        assert invalid and all(c["priority"] == "P2" for c in invalid)


class TestRuleOutputs:
    def test_boundary_cases_carry_value_and_unit(self):
        cases = mock_engine.generate(_full_profile())
        boundary_titles = [c["title"] for c in cases if c["method"] == "边界值"]
        assert any("8" in t for t in boundary_titles)
        assert any("20" in t for t in boundary_titles)
        assert any("5" in t for t in boundary_titles)

    def test_api_feature_maps_to_interface_category(self):
        cases = mock_engine.generate(_full_profile())
        api_case = next(c for c in cases if "POST /pet" in c["title"] and "有效等价类" in c["title"])
        assert api_case["category"] == "接口"

    def test_module_path_format(self):
        for c in mock_engine.generate(_full_profile()):
            assert "/" in c["module"], "所属模块应为 父模块/子模块 路径"

    def test_steps_multiline_format(self):
        for c in mock_engine.generate(_full_profile()):
            assert "\n" in c["steps"] or c["steps"].startswith("1.")


class TestPadding:
    def test_sparse_profile_pads_to_min_cases(self):
        profile = DocumentProfile(
            source="a.md",
            source_type="prd",
            module="空文档",
            features=[],
            boundaries=[],
            flows=[],
        )
        cases = mock_engine.generate(profile)
        assert len(cases) >= mock_engine.MIN_CASES
        assert len({c["method"] for c in cases}) >= mock_engine.MIN_METHOD_COVERAGE
        assert len({c["category"] for c in cases}) >= mock_engine.MIN_CATEGORY_COVERAGE

    def test_boundary_kind_texts(self):
        b_min = Boundary(text="长度下限", value=8, kind="range_min", unit="位")
        b_max = Boundary(text="长度上限", value=20, kind="range_max", unit="位")
        b_thr = Boundary(text="锁定阈值", value=5, kind="threshold", unit="次")
        assert "7" in mock_engine._boundary_cases(b_min, "M")[1]["title"]
        assert "21" in mock_engine._boundary_cases(b_max, "M")[1]["title"]
        assert "阈值相邻值" in mock_engine._boundary_cases(b_thr, "M")[1]["title"]

    def test_fmt_num(self):
        assert mock_engine._fmt_num(8.0) == "8"
        assert mock_engine._fmt_num(1.5) == "1.5"


class TestBoundaryExpectedQuality:
    """UP-3：边界值用例 expected 必须含具体数值、两侧值与方向，禁止通用套话。"""

    @pytest.mark.parametrize(
        "kind, value, unit",
        [
            ("range_min", 8, "位"),
            ("range_max", 20, "位"),
            ("threshold", 5, "次"),
        ],
    )
    def test_expected_contains_value_and_both_sides(self, kind, value, unit):
        b = Boundary(text="边界", value=value, kind=kind, unit=unit)
        for case in mock_engine._boundary_cases(b, "M"):
            exp = case["expected"]
            # 具体数值 + 两侧相邻值（v-1 / v / v+1）
            for num in (value - 1, value, value + 1):
                assert mock_engine._fmt_num(num) in exp, f"expected 缺少数值 {num}：{exp}"
            # 方向明确（接受 / 拒绝 / 触发 / 未达 之一）
            assert any(w in exp for w in ("接受", "拒绝", "触发", "未达")), exp

    def test_expected_not_generic_boilerplate(self):
        b = Boundary(text="阈值", value=5, kind="threshold", unit="次")
        for case in mock_engine._boundary_cases(b, "M"):
            assert "系统行为符合需求规则定义" not in case["expected"]
            assert "功能表现正常" not in case["expected"]

    def test_threshold_example_numbers(self):
        """示例锚点：输入 5 被接受，4 与 6 是两侧值（题目验收样例）。"""
        b = Boundary(text="锁定阈值", value=5, kind="threshold", unit="次")
        inside = mock_engine._boundary_cases(b, "M")[0]
        for num in ("5", "4", "6"):
            assert num in inside["expected"]

    def test_padded_boundary_case_also_has_numbers(self):
        """补齐用例若落到「边界值」，同样必须带具体数值与两侧值。"""
        profile = DocumentProfile(
            source="a.md",
            source_type="prd",
            module="空文档",
            features=[],
            boundaries=[],
            flows=[],
        )
        for case in mock_engine.generate(profile):
            if case["method"] == "边界值":
                for num in ("0", "1", "2"):
                    assert num in case["expected"]
                assert any(w in case["expected"] for w in ("接受", "拒绝"))

    def test_deterministic_expected(self):
        b = Boundary(text="阈值", value=5, kind="threshold", unit="次")
        first = mock_engine._boundary_cases(b, "M")
        second = mock_engine._boundary_cases(b, "M")
        assert first == second
