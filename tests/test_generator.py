"""生成编排端到端测试：3 个固定样例离线跑通（UP-3 / UP-1）。"""

import pytest

from casepilot.core.generator import detect_source_type, generate_from_file, parse_document
from casepilot.core.schema import CATEGORIES, METHODS, GenerationResult, TestCase
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, PETSTORE_OPENAPI

SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD]


class TestDetectSourceType:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("a.md", "prd"),
            ("a.txt", "prd"),
            ("a.yaml", "openapi"),
            ("a.yml", "openapi"),
            ("a.json", "openapi"),
        ],
    )
    def test_known_suffixes(self, name, expected):
        assert detect_source_type(name) == expected

    def test_unsupported_suffix_raises(self):
        with pytest.raises(ValueError) as exc:
            detect_source_type("a.docx")
        assert "暂不支持的输入格式" in str(exc.value)

    def test_no_suffix_raises(self):
        with pytest.raises(ValueError):
            detect_source_type("noext")


class TestParseDocument:
    @pytest.mark.parametrize("path", SAMPLES)
    def test_parse_all_samples(self, path):
        profile = parse_document(path)
        assert profile.features or profile.boundaries
        assert profile.module

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError) as exc:
            parse_document("不存在的文件.md")
        assert "不存在" in str(exc.value)


class TestGenerateFromSamples:
    """UP-3 验收：3 样例各产出合法用例集，method / category 覆盖达标。"""

    @pytest.mark.parametrize("path", SAMPLES)
    def test_end_to_end_offline(self, path, offline_env):
        result = generate_from_file(path)
        assert isinstance(result, GenerationResult)
        assert result.mode == "mock"
        assert result.source == str(path)

        # 结构合法率 100%：全部用例均为合法 TestCase
        assert result.cases
        for case in result.cases:
            assert isinstance(case, TestCase)

        # 每样例 ≥8 条用例
        assert len(result.cases) >= 8

        # method 覆盖 ≥3 种且非空
        methods = {c.method for c in result.cases}
        assert len(methods) >= 3
        assert methods <= set(METHODS)
        assert all(c.method for c in result.cases)

        # category 覆盖 ≥3 种
        categories = {c.category for c in result.cases}
        assert len(categories) >= 3
        assert categories <= set(CATEGORIES)

    @pytest.mark.parametrize("path", SAMPLES)
    def test_deterministic_cases(self, path, offline_env):
        first = generate_from_file(path, mode="mock")
        second = generate_from_file(path, mode="mock")
        assert [c.model_dump() for c in first.cases] == [c.model_dump() for c in second.cases]

    def test_module_parent_child_path(self, offline_env):
        result = generate_from_file(LOGIN_PRD, mode="mock")
        for case in result.cases:
            assert "/" in case.module


class TestValidationFailure:
    def test_invalid_raw_case_rejected(self):
        from casepilot.core.schema import validate_cases

        with pytest.raises(ValueError) as exc:
            validate_cases([{"title": "缺字段的用例"}])
        assert "第 1 条" in str(exc.value)
