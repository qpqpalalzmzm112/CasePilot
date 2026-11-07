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


class TestQualityGateIntegration:
    """覆盖维度检查集成到生成流程（UP-3），自动执行并输出中文结果。"""

    @pytest.mark.parametrize("path", SAMPLES)
    def test_coverage_report_logged_for_samples(self, path, offline_env, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="casepilot"):
            generate_from_file(path, mode="mock")
        assert "覆盖维度检查：共" in caplog.text
        assert "覆盖维度检查结论：达标" in caplog.text

    def test_warning_with_gap_details_when_not_met(self, offline_env, caplog, monkeypatch):
        import logging

        # 构造不达标输入：仅 3 条、2 种方法论
        weak_cases = [
            {
                "title": f"用例{i}",
                "precondition": "p",
                "steps": "1. s",
                "expected": "e",
                "priority": "P1",
                "category": "功能",
                "method": method,
                "module": "m/x",
            }
            for i, method in enumerate(("等价类", "等价类", "场景法"), 1)
        ]
        monkeypatch.setattr(
            "casepilot.core.llm.LLMGateway.generate_raw_cases",
            lambda self, profile: weak_cases,
        )
        with caplog.at_level(logging.WARNING, logger="casepilot"):
            generate_from_file(LOGIN_PRD, mode="mock")
        assert "覆盖维度检查结论：未达标" in caplog.text
        assert "差距明细如下" in caplog.text
        assert "用例数量 3 条" in caplog.text
        assert "方法论覆盖仅 2 种" in caplog.text


class TestBoundaryExpectedNumbers:
    """UP-3 验收：3 样例边界值用例 expected 含具体数值（≥90% 含对应数字）。"""

    @pytest.mark.parametrize("path", SAMPLES)
    def test_boundary_expected_carry_corresponding_numbers(self, path, offline_env):
        from casepilot.core import mock_engine
        from casepilot.core.generator import parse_document

        profile = parse_document(path)
        raw_cases = mock_engine.generate(profile)
        boundary_cases = [c for c in raw_cases if c["method"] == "边界值"]
        assert boundary_cases, f"{path} 应生成边界值用例"

        # 规则生成的边界值用例按「边界顺序、每边界 2 条」排列
        anchors = profile.boundaries[: mock_engine.MAX_BOUNDARIES]
        matched = 0
        for idx, case in enumerate(boundary_cases):
            if idx // 2 >= len(anchors):
                break  # 补齐用例不参与对应数字匹配
            b = anchors[idx // 2]
            nums = [
                mock_engine._fmt_num(b.value - 1),
                mock_engine._fmt_num(b.value),
                mock_engine._fmt_num(b.value + 1),
            ]
            if all(n in case["expected"] for n in nums):
                matched += 1
        ratio = matched / len(boundary_cases)
        assert ratio >= 0.9, f"{path} 边界值用例 expected 含对应数字比例仅 {ratio:.0%}"
