"""evals/metrics.py 4 维评分器与 golden 加载校验测试（全部离线、断言分数变化方向）。"""

from __future__ import annotations

import json

import pytest

from casepilot.core.schema import TestCase
from evals import metrics
from evals.metrics import (
    GoldenCase,
    boundary_numeric_rate,
    evaluate,
    find_golden_path,
    golden_overlap_rate,
    load_golden,
    method_coverage_rate,
    structure_validity_rate,
)

BASE_CASE = {
    "title": "【等价类】登录 - 有效等价类验证",
    "precondition": "账号 testuser@example.com 已注册且状态正常",
    "steps": "1. 输入账号与密码\n2. 点击登录\n3. 观察响应",
    "expected": "输入合法账号密码后登录成功并跳转首页",
    "priority": "P1",
    "category": "功能",
    "method": "等价类",
    "module": "用户登录模块需求文档/登录功能",
}


def make_case(**over) -> dict:
    raw = dict(BASE_CASE)
    raw.update(over)
    return raw


def make_golden(**over) -> GoldenCase:
    raw = make_case(**over)
    return GoldenCase(case=TestCase.model_validate(raw), note="设计理由")


class TestLoadGolden:
    def test_load_real_dataset(self):
        golden = load_golden(metrics.DATASETS_DIR / "prd_login_golden.json")
        assert len(golden) >= 15
        assert all(isinstance(g, GoldenCase) for g in golden)
        assert all(g.note for g in golden), "每条基准用例应带设计理由 note"

    def test_note_is_stripped_from_case_fields(self):
        golden = load_golden(metrics.DATASETS_DIR / "prd_login_golden.json")
        # note 已从字段中剥离，case 八要素通过 pydantic 校验
        assert golden[0].case.title.startswith("【")

    def test_missing_file_raises_chinese_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="基准集文件不存在"):
            load_golden(tmp_path / "none_golden.json")

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "bad_golden.json"
        p.write_text("{ 不是合法 json", encoding="utf-8")
        with pytest.raises(ValueError, match="不是合法 JSON"):
            load_golden(p)

    def test_non_array_raises(self, tmp_path):
        p = tmp_path / "obj_golden.json"
        p.write_text(json.dumps({"title": "x"}), encoding="utf-8")
        with pytest.raises(ValueError, match="非空 JSON 数组"):
            load_golden(p)

    def test_bad_enum_reports_index(self, tmp_path):
        bad = make_case(priority="P9")
        p = tmp_path / "enum_golden.json"
        p.write_text(json.dumps([bad]), encoding="utf-8")
        with pytest.raises(ValueError, match="第 1 条"):
            load_golden(p)

    def test_unknown_extra_field_rejected(self, tmp_path):
        bad = make_case()
        bad["unknown_field"] = "x"
        p = tmp_path / "extra_golden.json"
        p.write_text(json.dumps([bad]), encoding="utf-8")
        with pytest.raises(ValueError, match="第 1 条"):
            load_golden(p)

    def test_find_golden_path_auto_match(self):
        path = find_golden_path("examples/prd_login.md")
        assert path.name == "prd_login_golden.json"

    def test_find_golden_path_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="未找到基准集"):
            find_golden_path("examples/no_such.md", tmp_path)


class TestStructureValidityRate:
    def test_all_valid_scores_one(self):
        result = structure_validity_rate([make_case(), make_case(title="【边界值】密码 8 位边界")])
        assert result.score == 1.0

    def test_invalid_case_lowers_score(self):
        bad = make_case(priority="P9")  # 枚举非法
        result = structure_validity_rate([make_case(), bad])
        assert result.score == 0.5
        assert any("title" in g or "（" in g for g in result.gaps)

    def test_empty_set_scores_zero(self):
        result = structure_validity_rate([])
        assert result.score == 0.0
        assert result.gaps


class TestMethodCoverageRate:
    def test_full_coverage(self):
        golden = [make_golden(method=m) for m in ("等价类", "边界值", "场景法")]
        generated = [make_case(method=m) for m in ("等价类", "边界值", "场景法")]
        result = method_coverage_rate(golden, generated)
        assert result.score == 1.0

    def test_missing_method_lowers_score(self):
        golden = [make_golden(method=m) for m in ("等价类", "边界值", "场景法")]
        generated = [make_case(method="等价类")]
        result = method_coverage_rate(golden, generated)
        assert result.score == pytest.approx(1 / 3)
        assert any("边界值" in g for g in result.gaps)

    def test_more_coverage_scores_higher(self):
        golden = [make_golden(method=m) for m in ("等价类", "边界值", "场景法", "判定表")]
        two = method_coverage_rate(golden, [make_case(method="等价类"), make_case(method="边界值")])
        four = method_coverage_rate(
            golden, [make_case(method=m) for m in ("等价类", "边界值", "场景法", "判定表")]
        )
        assert four.score > two.score


class TestBoundaryNumericRate:
    def test_concrete_numbers_score_one(self):
        cases = [make_case(method="边界值", expected="密码 8 位被接受，7 位被拒绝")]
        assert boundary_numeric_rate(cases).score == 1.0

    def test_boilerplate_expected_scores_zero(self):
        cases = [make_case(method="边界值", title="【边界值】密码长度边界", expected="系统行为符合预期")]
        result = boundary_numeric_rate(cases)
        assert result.score == 0.0
        assert any("密码长度边界" in g for g in result.gaps)

    def test_no_boundary_cases_scores_zero(self):
        assert boundary_numeric_rate([make_case(method="等价类")]).score == 0.0

    def test_mixed_ratio(self):
        cases = [
            make_case(method="边界值", expected="8 位通过"),
            make_case(method="边界值", title="【边界值】另一条", expected="符合预期"),
            make_case(method="等价类", expected="无数字也无关"),
        ]
        assert boundary_numeric_rate(cases).score == 0.5


class TestGoldenOverlapRate:
    def test_same_triples_score_one(self):
        golden = [make_golden()]
        assert golden_overlap_rate(golden, [make_case()]).score == 1.0

    def test_category_mismatch_lowers_score(self):
        golden = [make_golden(), make_golden(title="【等价类】第二条", category="异常")]
        generated = [make_case(category="功能")]
        result = golden_overlap_rate(golden, generated)
        assert result.score == 0.5

    def test_loose_module_match(self):
        golden = [make_golden(module="用户登录模块需求文档/边界验证")]
        generated = [make_case(module="用户登录模块需求文档/补充验证")]  # 父模块相同，宽松匹配
        assert golden_overlap_rate(golden, generated).score == 1.0

    def test_unmatched_gap_cites_title(self):
        golden = [make_golden(method="判定表", title="【判定表】未覆盖场景")]
        result = golden_overlap_rate(golden, [make_case(method="等价类")])
        assert result.score == 0.0
        assert any("未覆盖场景" in g for g in result.gaps)


class TestEvaluateAndReport:
    def test_total_is_mean_x100(self):
        golden = [make_golden(method=m) for m in ("等价类", "边界值")]
        generated = [
            make_case(method="等价类", expected="8 位通过"),
            make_case(method="边界值", expected="8 位通过"),
        ]
        report = evaluate(generated, golden)
        dims = [d.score for d in report.dimensions]
        assert report.total == round(sum(dims) / len(dims) * 100, 1)

    def test_report_is_deterministic(self):
        golden = [make_golden(method="等价类")]
        generated = [make_case()]
        kwargs = dict(
            input_name="examples/prd_login.md",
            golden_name="evals/datasets/prd_login_golden.json",
            golden_count=1,
            case_count=1,
            mode_note="生成模式：mock",
        )
        first = evaluate(generated, golden).format_report(**kwargs)
        second = evaluate(generated, golden).format_report(**kwargs)
        assert first == second

    def test_report_is_chinese_with_scores_and_suggestions(self):
        golden = [make_golden(method="边界值", title="【边界值】golden 未覆盖")]
        generated = [make_case(method="等价类", expected="符合预期")]
        report = evaluate(generated, golden)
        text = report.format_report(
            input_name="examples/prd_login.md",
            golden_name="evals/datasets/prd_login_golden.json",
            golden_count=1,
            case_count=1,
        )
        assert "总分" in text and "## 维度得分" in text and "## 改进建议" in text
        for name in metrics.DIMENSIONS:
            assert name in text
        assert any("golden 未覆盖" in s for s in report.suggestions), "建议应引用具体用例标题"
