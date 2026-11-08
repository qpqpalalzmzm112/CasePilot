"""禅道 Excel 导出测试：列结构断言（代码断言，非肉眼）。"""

from openpyxl import load_workbook

from casepilot.core.schema import TestCase
from casepilot.exporters.zentao_excel import (
    PRIORITY_MAP,
    SHEET_NAME,
    ZENTAO_HEADERS,
    export_zentao_excel,
)


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


class TestColumnStructure:
    def _export(self, tmp_path, cases):
        out = tmp_path / "cases.xlsx"
        export_zentao_excel(cases, out)
        return load_workbook(out)

    def test_sheet_name(self, tmp_path):
        wb = self._export(tmp_path, [_case()])
        assert wb.sheetnames == [SHEET_NAME] == ["用例"]

    def test_header_row_exact(self, tmp_path):
        wb = self._export(tmp_path, [_case()])
        ws = wb["用例"]
        headers = [ws.cell(row=1, column=i).value for i in range(1, len(ZENTAO_HEADERS) + 1)]
        assert headers == ["所属模块", "用例标题", "前置条件", "步骤", "预期结果", "优先级", "用例类型", "关键词"]

    def test_column_count_fixed(self, tmp_path):
        wb = self._export(tmp_path, [_case()])
        ws = wb["用例"]
        assert ws.max_column == 8

    def test_data_row_mapping(self, tmp_path):
        wb = self._export(tmp_path, [_case()])
        ws = wb["用例"]
        row = [ws.cell(row=2, column=i).value for i in range(1, 9)]
        assert row == [
            "登录模块/场景验证",  # 所属模块（父模块/子模块）
            "验证登录成功",       # 用例标题
            "用户已注册",         # 前置条件
            "1. 输入账号密码\n2. 点击登录",  # 步骤（多步骤换行）
            "登录成功并跳转首页",  # 预期结果
            2,                   # 优先级 P1→2
            "功能",              # 用例类型（category 中文值）
            "场景法",            # 关键词（method 显式标注）
        ]

    def test_multiple_cases_written(self, tmp_path):
        wb = self._export(tmp_path, [_case(), _case(title="第二条")])
        ws = wb["用例"]
        assert ws.max_row == 3  # 表头 + 2 条
        assert ws.cell(row=3, column=2).value == "第二条"


class TestPriorityMapping:
    def test_mapping_table(self):
        assert PRIORITY_MAP == {"P0": 1, "P1": 2, "P2": 3, "P3": 4}

    def test_all_priorities_exported(self, tmp_path):
        cases = [_case(priority=p) for p in ("P0", "P1", "P2", "P3")]
        out = tmp_path / "p.xlsx"
        export_zentao_excel(cases, out)
        ws = load_workbook(out)["用例"]
        values = [ws.cell(row=i, column=6).value for i in range(2, 6)]
        assert values == [1, 2, 3, 4]


class TestContentType:
    def test_method_goes_to_keyword_column(self, tmp_path):
        for method in ("等价类", "边界值", "场景法", "判定表"):
            out = tmp_path / f"{method}.xlsx"
            export_zentao_excel([_case(method=method)], out)
            ws = load_workbook(out)["用例"]
            assert ws.cell(row=2, column=8).value == method

    def test_category_goes_to_type_column(self, tmp_path):
        for category in ("功能", "接口", "异常", "边界"):
            out = tmp_path / f"{category}.xlsx"
            export_zentao_excel([_case(category=category)], out)
            ws = load_workbook(out)["用例"]
            assert ws.cell(row=2, column=7).value == category

    def test_output_dir_created(self, tmp_path):
        out = tmp_path / "nested" / "dir" / "cases.xlsx"
        export_zentao_excel([_case()], out)
        assert out.exists()
