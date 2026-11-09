"""迭代 9 / UP-3 多阈值边界完整性 + 时间点切换边界回归（全离线）。

验收标准（DoD 4）：
- 同一条款多个数字阈值（「单笔超 10000 或当日累计 50000 触发短信」）→ 每个阈值
  都产出边界用例（含两侧相邻值），不得只测第一个；
- 时间点类边界（「07:00/22:00 电价切换」）→ 产出切换时刻前 / 时刻整 / 时刻后用例；
- 两个场景固化为回归测试，mock 确定性保持。
"""

from __future__ import annotations

import pytest

from casepilot.adapters import prd as prd_adapter
from casepilot.core import mock_engine
from casepilot.core.generator import generate_from_file, parse_document
from casepilot.core.schema import Boundary, DocumentProfile
from tests.conftest import EXAMPLES_DIR

LOGIN_FULL_PRD = EXAMPLES_DIR / "prd_login_full.md"

# 场景一：同条款双数值阈值（转账提醒，历史上只测了单笔 10000）
DUAL_THRESHOLD_PRD = (
    "# 转账提醒需求\n\n"
    "## 提醒规则\n\n"
    "- 单笔超 10000 或当日累计 50000 触发短信提醒\n"
)

# 场景二：时间点切换边界（分时电价）
TIMEPOINT_PRD = (
    "# 分时电价需求\n\n"
    "## 计费规则\n\n"
    "- 07:00/22:00 电价切换\n"
)


def _blob(cases) -> list[str]:
    return [f"{c['title']}\n{c['steps']}\n{c['expected']}" for c in cases]


class TestMultiThresholdPerClause:
    """同一条款多个阈值全部产出边界用例（含两侧相邻值）。"""

    def setup_method(self):
        self.profile = prd_adapter.parse_text(DUAL_THRESHOLD_PRD, source="dual.md")
        self.raw = mock_engine.generate(self.profile)
        self.blobs = _blob(self.raw)

    def test_both_thresholds_extracted(self):
        values = {b.value for b in self.profile.boundaries}
        assert values == {10000.0, 50000.0}, "单笔 10000 与累计 50000 都必须抽出"
        assert all(b.clause for b in self.profile.boundaries), "阈值须归属同一条款原文行"

    def test_each_threshold_has_adjacent_values(self):
        """DoD 4：10000 与 50000 各自产出含两侧相邻值的边界用例。"""
        joined = "\n".join(self.blobs)
        for nums in (("9999", "10000", "10001"), ("49999", "50000", "50001")):
            for n in nums:
                assert n in joined, f"双阈值场景缺少数值 {n}（不得只测第一个阈值）"

    def test_second_threshold_not_dropped(self):
        """第二个阈值（当日累计 50000）有独立用例，不因「只测第一个」漏掉。"""
        hits = [t for t in self.blobs if "50000" in t]
        assert len(hits) >= 2, "累计 50000 阈值应有自己的边界值 + 越界值用例"

    def test_multi_threshold_clause_group_selection(self):
        """成组选取：同条款阈值整组纳入（可略超预算），不被逐条截断（_select_boundaries）。"""
        profile = DocumentProfile(
            source="a.md",
            source_type="prd",
            module="多阈值",
            boundaries=[
                Boundary(text="条款甲 1 件", value=1, kind="threshold", unit="件", clause="条款甲 1 件"),
                Boundary(text="条款乙 2 件", value=2, kind="threshold", unit="件", clause="条款乙 2 件"),
                Boundary(text="条款丙 5 次", value=5, kind="threshold", unit="次", clause="条款丙 5 次 6 次 7 次 8 次"),
                Boundary(text="条款丙 6 次", value=6, kind="threshold", unit="次", clause="条款丙 5 次 6 次 7 次 8 次"),
                Boundary(text="条款丙 7 次", value=7, kind="threshold", unit="次", clause="条款丙 5 次 6 次 7 次 8 次"),
                Boundary(text="条款丙 8 次", value=8, kind="threshold", unit="次", clause="条款丙 5 次 6 次 7 次 8 次"),
                Boundary(text="条款丁 9 次", value=9, kind="threshold", unit="次", clause="条款丁 9 次"),
            ],
        )
        selected = mock_engine._select_boundaries(profile)
        assert [b.value for b in selected] == [1.0, 2.0, 5.0, 6.0, 7.0, 8.0], (
            "条款丙的 4 个阈值必须整组纳入（即使略超 MAX_BOUNDARIES 预算），"
            "条款丁在预算外整组排除"
        )


class TestTimepointBoundary:
    """时间点切换边界：时刻前 / 时刻整 / 时刻后三时点用例（DoD 4）。"""

    def setup_method(self):
        self.profile = prd_adapter.parse_text(TIMEPOINT_PRD, source="tp.md")
        self.raw = mock_engine.generate(self.profile)
        self.blobs = _blob(self.raw)
        self.joined = "\n".join(self.blobs)

    def test_timepoints_extracted(self):
        assert [(b.value, b.kind, b.unit) for b in self.profile.boundaries] == [
            (420.0, "timepoint", "时点"),
            (1320.0, "timepoint", "时点"),
        ]

    def test_switch_moment_covered(self):
        for point in ("07:00", "22:00"):
            assert point in self.joined, f"缺少切换时刻整 {point} 用例"

    def test_before_and_after_moments_covered(self):
        """DoD 4：切换时刻前 / 时刻后均须出现（06:59 / 07:01、21:59 / 22:01）。"""
        for point in ("06:59", "07:01", "21:59", "22:01"):
            assert point in self.joined, f"缺少切换相邻时刻 {point}"

    def test_each_timepoint_has_two_cases(self):
        cases_07 = [c for c in self.raw if "07:00" in c["title"]]
        cases_22 = [c for c in self.raw if "22:00" in c["title"]]
        assert len(cases_07) >= 2 and len(cases_22) >= 2, "每个时间点产边界值 + 相邻时刻用例"

    def test_before_at_after_semantics(self):
        """三时点语义：前=切换未生效、整=切换生效、后=保持生效。"""
        hit = next(t for t in self.blobs if "切换时刻整" in t and "07:00" in t)
        assert "切换未生效" in hit and "切换生效" in hit and "保持切换生效后的状态" in hit


class TestRegressionScenarios:
    """回归固化：双阈值 / 时间点场景 mock 确定性；既有样例多阈值不再被截断。"""

    @pytest.mark.parametrize(
        "text", [DUAL_THRESHOLD_PRD, TIMEPOINT_PRD], ids=["dual", "timepoint"]
    )
    def test_mock_deterministic(self, text):
        profile = prd_adapter.parse_text(text, source="x.md")
        assert mock_engine.generate(profile) == mock_engine.generate(profile)

    def test_login_full_multi_threshold_not_truncated(self):
        """prd_login_full 的 FR-3 条款含 6 个阈值：60 秒 / 3 次不再被「只测第一个」漏掉。"""
        result = generate_from_file(LOGIN_FULL_PRD, mode="mock")
        titles = [c.title for c in result.cases]
        assert any("60 秒" in t and "阈值" in t for t in titles), "验证码 60 秒阈值应产出边界用例"
        assert any("3 次" in t and "阈值" in t for t in titles), "连续 3 次输错阈值应产出边界用例"

    def test_full_pipeline_dual_threshold(self, tmp_path, monkeypatch, offline_env):
        """端到端：双阈值文档生成后每条阈值都在对账覆盖范围内。"""
        monkeypatch.chdir(tmp_path)
        doc = tmp_path / "dual.md"
        doc.write_text(DUAL_THRESHOLD_PRD, encoding="utf-8")
        result = generate_from_file(doc, mode="mock")
        joined = "\n".join(f"{c.title}\n{c.expected}" for c in result.cases)
        for n in ("9999", "10000", "10001", "49999", "50000", "50001"):
            assert n in joined
