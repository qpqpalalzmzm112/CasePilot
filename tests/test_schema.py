"""数据模型与结构校验测试（UP-1）。"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from casepilot.core.schema import (
    Boundary,
    DocumentProfile,
    Feature,
    GenerationResult,
    TestCase,
    validate_case,
    validate_cases,
)


def _case(**overrides) -> dict:
    base = {
        "title": "验证登录成功",
        "precondition": "用户已注册",
        "steps": "1. 输入账号密码\n2. 点击登录",
        "expected": "登录成功",
        "priority": "P0",
        "category": "功能",
        "method": "场景法",
        "module": "登录/场景验证",
    }
    base.update(overrides)
    return base


class TestTestCase:
    def test_valid_case_passes(self):
        case = TestCase.model_validate(_case())
        assert case.title == "验证登录成功"
        assert case.priority == "P0"

    def test_invalid_priority_rejected(self):
        with pytest.raises(ValidationError) as exc:
            TestCase.model_validate(_case(priority="P9"))
        assert "priority" in str(exc.value)

    def test_invalid_method_rejected(self):
        with pytest.raises(ValidationError) as exc:
            TestCase.model_validate(_case(method="随机测试"))
        assert "method" in str(exc.value)

    def test_invalid_category_rejected(self):
        with pytest.raises(ValidationError) as exc:
            TestCase.model_validate(_case(category="性能"))
        assert "category" in str(exc.value)

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError) as exc:
            TestCase.model_validate(_case(unknown_field="x"))
        assert "unknown_field" in str(exc.value)

    def test_empty_title_rejected(self):
        with pytest.raises(ValidationError):
            TestCase.model_validate(_case(title="   "))

    def test_whitespace_stripped(self):
        case = TestCase.model_validate(_case(title="  验证登录  "))
        assert case.title == "验证登录"

    @pytest.mark.parametrize("priority", ["P0", "P1", "P2", "P3"])
    def test_all_priorities_accepted(self, priority):
        assert TestCase.model_validate(_case(priority=priority)).priority == priority


class TestGenerationResult:
    def test_valid_result(self):
        result = GenerationResult(
            source="examples/prd_login.md",
            cases=[TestCase.model_validate(_case())],
            mode="mock",
            generated_at=datetime(2026, 1, 1),
        )
        assert result.mode == "mock"
        assert len(result.cases) == 1

    def test_empty_cases_rejected(self):
        with pytest.raises(ValidationError):
            GenerationResult(source="x", cases=[], mode="mock")

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValidationError) as exc:
            GenerationResult(
                source="x",
                cases=[TestCase.model_validate(_case())],
                mode="gpt",
            )
        assert "mode" in str(exc.value)

    def test_invalid_case_in_result_rejected(self):
        with pytest.raises(ValidationError):
            GenerationResult(source="x", cases=[{"title": "缺字段"}], mode="mock")


class TestValidateHelpers:
    def test_validate_case_ok(self, sample_case_data):
        assert validate_case(sample_case_data).title == "验证登录成功"

    def test_validate_case_error_in_chinese(self):
        with pytest.raises(ValueError) as exc:
            validate_case(_case(priority="P9"))
        assert "结构校验失败" in str(exc.value)

    def test_validate_cases_index_in_error(self):
        raw = [_case(), _case(priority="P5")]
        with pytest.raises(ValueError) as exc:
            validate_cases(raw)
        assert "第 2 条" in str(exc.value)


class TestDocumentProfile:
    def test_summary_text(self):
        profile = DocumentProfile(
            source="a.md",
            source_type="prd",
            module="登录",
            features=[Feature(name="支持登录", kind="function")],
            boundaries=[Boundary(text="密码 8-20 位", value=8, kind="range_min", unit="位")],
            flows=["打开登录页"],
        )
        text = profile.summary_text()
        assert "登录" in text
        assert "支持登录" in text
        assert "8" in text
        assert "1. 打开登录页" in text

    def test_summary_text_empty_sections(self):
        profile = DocumentProfile(source="a.md", source_type="prd", module="登录")
        assert "功能点" not in profile.summary_text()
