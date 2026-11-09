"""迭代 9 / UP-2 NFR 寄生幻觉专防 + 依据 / 假设标注进产物（全离线）。

验收标准（DoD 2）：
- 构造「支持 10 万台设备」NFR 条款 → 不得产出「触发限流降级 / 告警」类硬性预期；
  NFR 性能 / 容量条目只产「目标规模下满足判定标准」形态用例；
- LLM 模式产出的衍生 / 假设类用例经 check_grounding 判定 [假设] 后，
  关键词列标注「场景法,假设」（Excel 8 列不变）；[FR-x] 依据用例不受影响；
- pytest / Markdown 导出旁注「（设计假设）」；mock 不产此类（全样例零标注）；
- judge 逐条来源标注与 --strict 删除行为不变（test_grounding.py 已锁）。
"""

from __future__ import annotations

import inspect

import pytest

from casepilot.adapters import prd as prd_adapter
from casepilot.core import mock_engine, quality
from casepilot.core.generator import _tag_assumption_cases, generate_from_file
from casepilot.core.methodology import build_system_prompt
from casepilot.core.schema import TestCase
from casepilot.exporters.markdown_export import build_markdown
from casepilot.exporters.pytest_skeleton import build_skeleton
from casepilot.exporters.zentao_excel import (
    export_zentao_excel,
    keyword_value,
    split_keyword,
)
from casepilot.core.quality import validate_zentao_excel
from tests.conftest import (
    BOUNDARY_PRD,
    EXAMPLES_DIR,
    LOGIN_PRD,
    NUMBERED_PRD,
    PETSTORE_OPENAPI,
    SECURITY_PERF_PRD,
)

LOGIN_FULL_PRD = EXAMPLES_DIR / "prd_login_full.md"
ALL_SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD, SECURITY_PERF_PRD, NUMBERED_PRD, LOGIN_FULL_PRD]

# 构造 NFR 容量条款（DoD 2）：只定义目标规模，未定义超容行为
NFR_CAPACITY_PRD = (
    "# 物联网平台需求\n\n"
    "## 非功能需求\n\n"
    "NFR-1：支持 10 万台设备接入\n\n"
    "## 功能需求\n\n"
    "FR-1：支持设备上线注册\n"
)
# 寄生幻觉禁用词（超容行为类硬性预期，PRD 未定义即禁止出现）
PARASITIC_WORDS = ("限流", "降级", "熔断", "告警", "429")


def _case(**overrides) -> dict:
    base = {
        "title": "[FR-1] 验证设备上线注册",
        "precondition": "设备已完成配网",
        "steps": "1. 设备发起上线注册\n2. 观察注册结果",
        "expected": "支持设备上线注册：注册请求被接受并完成上线注册",
        "priority": "P1",
        "category": "功能",
        "method": "等价类",
        "module": "物联网平台/功能验证",
    }
    base.update(overrides)
    return base


class TestNFRParasiticGuard:
    """NFR 性能 / 容量条款：只产判定标准形态，禁止发明超容行为（DoD 2）。"""

    def setup_method(self):
        self.profile = prd_adapter.parse_text(NFR_CAPACITY_PRD, source="nfr.md")
        self.raw = mock_engine.generate(self.profile)

    def test_no_parasitic_hard_expectations(self):
        """「支持 10 万台设备」→ 产物不得出现限流 / 降级 / 熔断 / 告警 / 429 硬性预期。"""
        for case in self.raw:
            blob = f"{case['title']}\n{case['steps']}\n{case['expected']}"
            for word in PARASITIC_WORDS:
                assert word not in blob, f"{case['title']} 含寄生幻觉词「{word}」"

    def test_nfr_case_is_target_scale_form(self):
        """NFR 容量用例只验「目标规模下满足判定标准」，不作超容行为断言。"""
        target_cases = [
            c
            for c in self.raw
            if "目标规模" in c["title"] or "边界对照点" in c["title"] or "判定标准验证" in c["title"]
        ]
        assert target_cases, "NFR-1 容量阈值应产出目标规模 / 判定标准用例"
        for case in target_cases:
            assert case["method"] == "性能测试", case["title"]
            assert "目标规模" in case["expected"] or "判定标准" in case["expected"], case["title"]
        # 引用容量阈值的用例不得断言超出目标规模后的行为
        for case in self.raw:
            if "10 万台" in case["expected"] or "10万台" in case["expected"]:
                assert "被拒绝" not in case["expected"], case["title"]

    def test_capacity_clause_classified_as_performance(self):
        assert mock_engine.classify_requirement("支持 10 万台设备接入") == "性能测试"

    def test_mock_engine_source_free_of_parasitic_templates(self):
        source = inspect.getsource(mock_engine)
        for banned in ("限流", "熔断", "告警", "429"):
            assert banned not in source, f"mock 引擎残留寄生规则表述「{banned}」"

    def test_prompt_forbids_undefined_behavior(self):
        prompt = build_system_prompt()
        assert "PRD 未定义的行为一律不生成硬性预期" in prompt
        assert "禁止发明超出目标规模后的行为" in prompt
        assert "系统接受有效输入并处理成功" in prompt  # 同义反复示例随提示词禁用


class TestAssumptionTagging:
    """衍生 / 假设类用例标注「,假设」进关键词列（DoD 2）。"""

    def setup_method(self):
        self.source = NFR_CAPACITY_PRD
        self.profile = prd_adapter.parse_text(self.source, source="nfr.md")

    def _derived_case(self) -> TestCase:
        return TestCase.model_validate(
            _case(
                title="[FR-1] 验证超容行为（衍生设计）",
                expected="支持设备上线注册；触发限流降级，服务进入只读模式",
            )
        )

    def _grounded_case(self) -> TestCase:
        return TestCase.model_validate(_case())

    def test_derived_case_tagged_assumption(self):
        cases = [self._grounded_case(), self._derived_case()]
        tagged = _tag_assumption_cases(cases, self.source, self.profile)
        assert tagged[0].assumption is False, "[FR-x] 依据用例不受影响"
        assert tagged[1].assumption is True, "衍生用例（限流降级）必须标注假设"

    def test_keyword_column_renders_comma_assumption(self, tmp_path):
        """Excel 8 列不变，关键词列「场景法,假设」逗号并列。"""
        case = self._derived_case().model_copy(
            update={"assumption": True, "method": "场景法"}
        )
        out = tmp_path / "assume.xlsx"
        export_zentao_excel([case], out)
        report = validate_zentao_excel(out)
        assert report.ok, report.format_report()
        assert report.warnings == [], "「,假设」标注不应触发关键词枚举警告"

    def test_split_keyword_roundtrip(self):
        assert keyword_value("场景法", True) == "场景法,假设"
        assert split_keyword("场景法,假设") == ("场景法", True)
        assert split_keyword("场景法") == ("场景法", False)

    def test_pytest_skeleton_annotation(self):
        case = self._derived_case().model_copy(update={"assumption": True, "method": "场景法"})
        text = build_skeleton([case], source="nfr.md", generated_at="2026-01-01 00:00:00")
        assert "# 方法: 场景法（设计假设）" in text

    def test_markdown_annotation(self):
        case = self._derived_case().model_copy(update={"assumption": True, "method": "场景法"})
        text = build_markdown([case], source="nfr.md")
        assert "场景法（设计假设）" in text

    def test_mock_output_never_tagged(self):
        """mock 规则直出用例全部有据，0 条假设标注（DoD 2：mock 不产此类）。"""
        for path in ALL_SAMPLES:
            result = generate_from_file(path, mode="mock")
            assert all(not c.assumption for c in result.cases), path.name

    def test_measurement_prediction_not_downgraded(self):
        """测量值预言按禁令拦截，不降级为 [假设] 标注。"""
        cases = [
            TestCase.model_validate(
                _case(title="[FR-1] 验证性能", expected="平均响应时间为 1.8 秒")
            )
        ]
        tagged = _tag_assumption_cases(cases, self.source, self.profile)
        assert tagged[0].assumption is False

    def test_grounding_label_report_unchanged(self):
        """judge 逐条来源标注（[PRD §编号] / [假设]）保留（迭代 8 行为不变）。"""
        report = quality.check_grounding(
            [self._grounded_case(), self._derived_case()], self.source, self.profile.frs
        )
        labels = {s.index: s.label for s in report.sources}
        assert labels[1] == "[PRD §FR-1]"
        assert labels[2] == quality.LABEL_ASSUMPTION
