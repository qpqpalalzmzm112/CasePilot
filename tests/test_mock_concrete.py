"""UP-3 验收测试：mock 引擎等价类 / 场景法预期结果必须引用输入的具体词或数值。"""

from __future__ import annotations

from pathlib import Path

import pytest

from casepilot.core import mock_engine
from casepilot.core.generator import parse_document

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
SAMPLES = (
    EXAMPLES / "prd_login.md",
    EXAMPLES / "openapi_petstore.yaml",
    EXAMPLES / "prd_boundary.md",
)


def _concrete_tokens(profile) -> list[str]:
    """从输入文档抽取的具体词 / 数值：功能点名称、主流程步骤文本、边界描述。"""
    tokens = [f.name for f in profile.features]
    tokens.extend(profile.flows)
    tokens.extend(b.text for b in profile.boundaries)
    return [t for t in tokens if t]


def _generate_twice(path):
    profile = parse_document(path)
    first = mock_engine.generate(profile)
    second = mock_engine.generate(profile)
    assert first == second, "mock 生成必须确定性"
    return profile, first


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda p: p.stem)
class TestConcreteExpected:
    def test_equivalence_and_scenario_concrete_ratio(self, sample):
        """等价类与场景法用例 expected 含输入具体词 / 数值比例 ≥80%。"""
        profile, cases = _generate_twice(sample)
        tokens = _concrete_tokens(profile)
        assert tokens, f"{sample.name} 应能从输入抽取具体词"
        scoped = [c for c in cases if c["method"] in ("等价类", "场景法")]
        assert scoped, f"{sample.name} 应生成等价类 / 场景法用例"
        hits = [c for c in scoped if any(t in c["expected"] for t in tokens)]
        ratio = len(hits) / len(scoped)
        assert ratio >= 0.8, (
            f"{sample.name} 等价类/场景法具体值比例 {ratio:.0%} 低于 80%，"
            f"未命中：{[c['title'] for c in scoped if c not in hits]}"
        )

    def test_equivalence_expected_cites_feature_name(self, sample):
        profile, cases = _generate_twice(sample)
        names = {f.name for f in profile.features}
        equivalence = [c for c in cases if c["method"] == "等价类" and c["title"].startswith("【等价类】")]
        # 补齐产生的等价类用例（如有）引用锚点，规则产生的必须引用功能点名称
        rule_cases = [c for c in equivalence if "有效等价类验证" in c["title"] or "无效等价类验证" in c["title"]]
        for c in rule_cases:
            assert any(n in c["expected"] for n in names), f"等价类预期应引用功能点名称：{c['title']}"

    def test_scenario_expected_cites_scenario_element(self, sample):
        profile, cases = _generate_twice(sample)
        tokens = _concrete_tokens(profile)
        scenario = [c for c in cases if c["method"] == "场景法"]
        for c in scenario:
            assert any(t in c["expected"] for t in tokens), f"场景法预期应引用场景要素：{c['title']}"

    def test_no_pure_template_expected(self, sample):
        _, cases = _generate_twice(sample)
        banned = (
            "系统接受有效等价类输入，功能执行成功，返回结果符合预期",
            "系统拒绝无效等价类输入，给出明确中文错误提示，数据不被写入",
            "各步骤顺序执行成功，业务目标顺利达成，最终状态正确",
        )
        for c in cases:
            if c["method"] in ("等价类", "场景法"):
                assert c["expected"] not in banned, f"纯模板套话已禁用：{c['title']}"

    def test_boundary_expected_keeps_numbers(self, sample):
        _, cases = _generate_twice(sample)
        boundary = [c for c in cases if c["method"] == "边界值"]
        for c in boundary:
            assert any(ch.isdigit() for ch in c["expected"]), f"边界值预期应含具体数值：{c['title']}"
