"""质量门禁测试：覆盖维度检查 + 禅道导入前校验器（UP-2 / UP-3，全离线）。"""

from openpyxl import Workbook, load_workbook

from casepilot.core import mock_engine
from casepilot.core.quality import (
    CoverageReport,
    MIN_CASES,
    ZentaoImportReport,
    check_coverage,
    format_coverage_report,
    validate_zentao_excel,
)
from casepilot.core.schema import DocumentProfile, Feature, TestCase
from casepilot.exporters.zentao_excel import ZENTAO_HEADERS, export_zentao_excel
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, PETSTORE_OPENAPI

SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD]


def _case(**overrides) -> TestCase:
    base = {
        "title": "验证登录成功",
        "precondition": "用户已注册",
        "steps": "1. 输入账号密码\n2. 点击登录",
        "expected": "登录成功并跳转首页",
        "priority": "P1",
        "category": "功能",
        "method": "场景法",
        "module": "登录模块/场景验证",
    }
    base.update(overrides)
    return TestCase.model_validate(base)


class TestCheckCoverage:
    def test_ok_when_all_dimensions_met(self):
        profile = DocumentProfile(
            source="a.md",
            source_type="prd",
            module="登录模块",
            features=[Feature(name="支持账号密码登录")],
            flows=["打开登录页", "点击登录"],
        )
        report = check_coverage(mock_engine.generate(profile))
        assert report.ok
        assert report.total >= MIN_CASES
        assert len(report.methods) >= 3
        assert len(report.categories) >= 3
        assert report.empty_method_count == 0

    def test_too_few_cases_reports_gap(self):
        report = check_coverage([_case()] * 6)
        assert not report.ok
        text = format_coverage_report(report)
        assert "用例数量 6 条" in text
        assert f"低于下限 {MIN_CASES} 条" in text
        assert "还差 2 条" in text

    def test_method_coverage_gap_lists_missing(self):
        cases = [_case(method="等价类"), _case(method="场景法")]
        report = check_coverage(cases)
        assert not report.ok
        text = format_coverage_report(report)
        assert "方法论覆盖仅 2 种" in text
        assert "缺少：边界值、判定表" in text

    def test_category_coverage_gap_lists_missing(self):
        cases = [
            _case(method=m, category=c)
            for m, c in (("等价类", "功能"), ("边界值", "边界"), ("场景法", "功能"))
        ]
        report = check_coverage(cases)
        assert not report.ok
        text = format_coverage_report(report)
        assert "用例类型覆盖仅 2 种" in text

    def test_empty_method_flagged(self):
        raw = [
            {"title": "t", "method": "等价类", "category": "功能"},
            {"title": "t2", "method": "", "category": "异常"},
            {"title": "t3", "method": "边界值", "category": "边界"},
        ]
        report = check_coverage(raw)
        assert not report.ok
        assert report.empty_method_count == 1
        assert any("方法论标注存在空值" in issue for issue in report.issues)

    def test_report_format_is_chinese(self):
        cases = [
            _case(method="等价类", category="功能"),
            _case(method="边界值", category="边界"),
            _case(method="场景法", category="功能"),
            _case(method="判定表", category="异常"),
            _case(method="等价类", category="功能"),
            _case(method="边界值", category="边界"),
            _case(method="场景法", category="异常"),
            _case(method="判定表", category="接口"),
        ]
        text = format_coverage_report(check_coverage(cases))
        assert "覆盖维度检查：共 8 条用例" in text
        assert "（达标）" in text
        assert "覆盖维度检查结论：达标" in text

    def test_unmet_report_lists_all_gaps(self):
        report = check_coverage([_case()] * 2)
        text = format_coverage_report(report)
        assert "覆盖维度检查结论：未达标，差距明细如下：" in text
        assert "用例数量 2 条" in text
        assert "方法论覆盖仅 1 种" in text


class TestValidateZentaoExcel:
    def test_exported_file_is_importable(self, tmp_path):
        out = tmp_path / "ok.xlsx"
        export_zentao_excel([_case(), _case(title="第二条")], out)
        report = validate_zentao_excel(out)
        assert report.ok
        assert report.checked_rows == 2
        text = report.format_report()
        assert "校验结论：可导入" in text
        assert "错误" not in text

    def test_sample_exports_are_importable(self, tmp_path, offline_env):
        from casepilot.core.generator import generate_from_file

        for sample in SAMPLES:
            result = generate_from_file(sample, mode="mock")
            out = tmp_path / f"{sample.stem}.xlsx"
            export_zentao_excel(result.cases, out)
            report = validate_zentao_excel(out)
            assert report.ok, report.format_report()
            assert "校验结论：可导入" in report.format_report()

    def test_wrong_header_names_reported(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "用例"
        ws.append(["所属模块", "标题", "前置条目", "步骤", "预期结果", "优先级", "用例类型", "关键词"])
        ws.append(["登录/场景", "验证登录", "已注册", "1. 登录", "成功", 1, "功能", "场景法"])
        out = tmp_path / "bad_header.xlsx"
        wb.save(out)

        report = validate_zentao_excel(out)
        assert not report.ok
        text = report.format_report()
        assert "校验结论：不可导入" in text
        assert "第 2 列列名错误：期望「用例标题」，实际「标题」" in text
        assert "第 3 列列名错误：期望「前置条件」，实际「前置条目」" in text

    def test_illegal_priority_reported(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "用例"
        ws.append(ZENTAO_HEADERS)
        ws.append(["登录/场景", "验证登录", "已注册", "1. 登录", "成功", "高", "功能", "场景法"])
        out = tmp_path / "bad_priority.xlsx"
        wb.save(out)

        report = validate_zentao_excel(out)
        assert not report.ok
        text = report.format_report()
        assert "第 2 行「优先级」非法取值「高」" in text
        assert "合法取值：1/2/3/4" in text

    def test_missing_required_cell_reported(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "用例"
        ws.append(ZENTAO_HEADERS)
        ws.append(["登录/场景", "", "已注册", "", "成功", 2, "功能", "场景法"])
        out = tmp_path / "missing.xlsx"
        wb.save(out)

        report = validate_zentao_excel(out)
        assert not report.ok
        text = report.format_report()
        assert "第 2 行「用例标题」为空，禅道导入必填" in text
        assert "第 2 行「步骤」为空，禅道导入必填" in text

    def test_missing_sheet_reported(self, tmp_path):
        wb = Workbook()
        wb.active.title = "Sheet1"
        out = tmp_path / "no_sheet.xlsx"
        wb.save(out)

        report = validate_zentao_excel(out)
        assert not report.ok
        assert "缺少「用例」工作表" in report.format_report()

    def test_empty_data_reported(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "用例"
        ws.append(ZENTAO_HEADERS)
        out = tmp_path / "empty.xlsx"
        wb.save(out)

        report = validate_zentao_excel(out)
        assert not report.ok
        assert "用例数据为空" in report.format_report()

    def test_file_not_exist(self, tmp_path):
        report = validate_zentao_excel(tmp_path / "missing.xlsx")
        assert not report.ok
        assert "文件不存在" in report.format_report()

    def test_category_keyword_warnings_do_not_block(self, tmp_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "用例"
        ws.append(ZENTAO_HEADERS)
        ws.append(["登录/场景", "验证登录", "已注册", "1. 登录", "成功", 3, "自定义类型", "自定义关键词"])
        out = tmp_path / "warn.xlsx"
        wb.save(out)

        report = validate_zentao_excel(out)
        assert report.ok  # 警告不阻断导入
        text = report.format_report()
        assert "警告" in text
        assert "校验结论：可导入" in text

    def test_report_types(self, tmp_path):
        out = tmp_path / "x.xlsx"
        export_zentao_excel([_case()], out)
        assert isinstance(validate_zentao_excel(out), ZentaoImportReport)
        assert isinstance(check_coverage([_case()] * 8), CoverageReport)
