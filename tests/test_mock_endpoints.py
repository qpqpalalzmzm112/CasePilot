"""迭代 4（UP-2）验收测试：OpenAPI /user 端点补齐与 golden 三元组全覆盖。

评测驱动改进闭环：petstore 基准要素重合度 17/19 → 19/19（总分 100.0），
且 /user 端点相关用例（创建用户等价类 / username 参数边界 / 场景法接口专项）齐备。
"""

from __future__ import annotations

import pytest

from casepilot.core import mock_engine
from casepilot.core.generator import parse_document
from casepilot.core.schema import Boundary, DocumentProfile, Feature, TestCase
from evals.metrics import DATASETS_DIR, evaluate, load_golden
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, PETSTORE_OPENAPI

# 迭代 3 基线生成用例数（用于「±5 条内」回归断言）
BASELINE_COUNTS = {
    LOGIN_PRD: 27,
    PETSTORE_OPENAPI: 27,
    BOUNDARY_PRD: 25,
}


def _generate(path):
    profile = parse_document(path)
    cases = mock_engine.generate(profile)
    # 新规则必须保持确定性（同输入同输出）
    assert cases == mock_engine.generate(profile)
    return profile, cases


class TestUserEndpointCoverage:
    """补齐 OpenAPI /user 端点相关用例生成（创建用户 / username 参数边界 / 接口场景）。"""

    def setup_method(self):
        self.profile, self.cases = _generate(PETSTORE_OPENAPI)

    def test_user_equivalence_case_generated(self):
        """创建用户（POST /user）等价类用例带方法论标注。"""
        hits = [
            c for c in self.cases
            if "POST /user" in c["title"] and "等价类" == c["method"]
        ]
        assert hits, "应生成 POST /user（创建用户）等价类用例"
        for c in hits:
            assert c["method"] in ("等价类",)
            assert c["category"] in ("接口", "异常")

    def test_user_scenario_interface_case_generated(self):
        """/user 资源应有「场景法 · 接口」专项用例（补齐接口类场景三元组）。"""
        hits = [
            c for c in self.cases
            if c["method"] == "场景法" and c["category"] == "接口" and "user" in c["title"]
        ]
        assert hits, "应生成 user 资源场景法接口专项用例"
        for c in hits:
            TestCase.model_validate(c)

    def test_user_boundary_case_cites_username_limits(self):
        """/user 参数边界专项须引用 username 长度边界（3 / 20 位）与两侧相邻值。"""
        hits = [
            c for c in self.cases
            if c["method"] == "边界值" and "user 资源参数边界专项" in c["title"]
        ]
        assert hits, "应生成 user 资源参数边界专项用例"
        expected = hits[0]["expected"]
        for num in ("2", "3", "4", "19", "20", "21"):
            assert num in expected, f"边界专项预期应含具体数值 {num}：{expected}"
        assert any(w in expected for w in ("接受", "拒绝")), expected

    def test_all_user_cases_have_method_label(self):
        """/user 相关用例必须带显式方法论标注（等价类 / 边界值 / 场景法）。"""
        user_cases = [c for c in self.cases if "user" in c["title"].lower() or "用户" in c["title"]]
        assert user_cases
        for c in user_cases:
            assert c["method"] in ("等价类", "边界值", "场景法", "判定表")


class TestGoldenTripleCoverage:
    """评测驱动闭环：petstore golden 19 条三元组全覆盖，总分 100.0。"""

    def test_petstore_overlap_19_of_19(self):
        _, cases = _generate(PETSTORE_OPENAPI)
        golden = load_golden(DATASETS_DIR / "openapi_petstore_golden.json")
        report = evaluate([c for c in cases], golden)
        overlap = next(d for d in report.dimensions if d.name == "基准要素重合度")
        assert overlap.score == 1.0, f"三元组应 19/19 全覆盖：{overlap.summary}"
        assert report.total == 100.0, f"petstore 总分应为 100.0：{report.total}"

    def test_prd_samples_not_regressed(self):
        for path, golden_name in (
            (LOGIN_PRD, "prd_login_golden.json"),
            (BOUNDARY_PRD, "prd_boundary_golden.json"),
        ):
            _, cases = _generate(path)
            golden = load_golden(DATASETS_DIR / golden_name)
            report = evaluate([c for c in cases], golden)
            assert report.total == 100.0, f"{path.name} 总分回退：{report.total}"

    def test_case_count_within_tolerance(self):
        """3 样例生成用例数变化在 ±5 条内。"""
        for path, baseline in BASELINE_COUNTS.items():
            _, cases = _generate(path)
            assert abs(len(cases) - baseline) <= 5, (
                f"{path.name} 用例数 {len(cases)} 相对基线 {baseline} 变化超过 ±5 条"
            )


class TestEndpointRuleUnits:
    """规则 6 内部工具与规模约束单测。"""

    def test_api_resource_parsing(self):
        assert mock_engine._api_resource("POST /user（创建用户）") == ("user", {"user"})
        assert mock_engine._api_resource("GET /pet/{petId}（按 ID 查询宠物）") == (
            "pet", {"pet", "petid"},
        )
        assert mock_engine._api_resource("POST /store/order（提交订单）") == (
            "store", {"store", "order"},
        )
        assert mock_engine._api_resource("支持账号密码登录") is None

    def test_resource_groups_keep_document_order(self):
        profile = parse_document(PETSTORE_OPENAPI)
        keys = [g.key for g in mock_engine._resource_groups(profile)]
        assert keys == ["pet", "store", "user"]

    def test_flow_covered_resources(self):
        profile = parse_document(PETSTORE_OPENAPI)
        groups = mock_engine._resource_groups(profile)
        covered = mock_engine._flow_covered_keys(profile, groups)
        assert "pet" in covered and "user" not in covered

    def test_prd_profile_gets_no_endpoint_cases(self):
        profile = parse_document(LOGIN_PRD)
        assert mock_engine._endpoint_cases(profile) == []

    def test_endpoint_cases_deterministic_and_bounded(self):
        profile = parse_document(PETSTORE_OPENAPI)
        cases = mock_engine._endpoint_cases(profile)
        assert cases == mock_engine._endpoint_cases(profile)
        # 场景专项 ≤ MAX_ENDPOINT_SCENARIOS，参数边界专项合并为 1 条
        scenario = [c for c in cases if c["method"] == "场景法"]
        boundary = [c for c in cases if c["method"] == "边界值"]
        assert len(scenario) <= mock_engine.MAX_ENDPOINT_SCENARIOS
        assert len(boundary) == 1
        assert len(boundary) <= 1  # 参数边界专项全文档合并 1 条，控制规模

    def test_no_gap_resource_no_extra_cases(self):
        """主流程覆盖全部资源时不再补专项（不灌水）。"""
        profile = DocumentProfile(
            source="api.yaml",
            source_type="openapi",
            module="单资源接口",
            features=[Feature(name="POST /user（创建用户）", kind="api")],
            boundaries=[Boundary(text="/user 参数 id 的 minimum 约束", value=1, kind="range_min")],
            flows=["调用 POST /user（创建用户）"],
        )
        assert mock_engine._endpoint_cases(profile) == []

    def test_merged_boundary_direction_text(self):
        b_min = Boundary(text="schema User.username 的 minLength 约束", value=3, kind="range_min", unit="字符")
        text = mock_engine._boundary_direction_text(b_min)
        for num in ("2", "3", "4"):
            assert num in text
        assert "接受" in text and "拒绝" in text
