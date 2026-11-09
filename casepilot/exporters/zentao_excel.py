"""禅道 Excel 导出（openpyxl）。

禅道导入模板列规范（Sheet 名「用例」，第一行表头，列顺序固定）：
所属模块 | 用例标题 | 前置条件 | 步骤 | 预期结果 | 优先级 | 用例类型 | 关键词

映射规则：
- 优先级：P0→1、P1→2、P2→3、P3→4（禅道数字优先级）
- 用例类型列：填 category 中文值（功能/接口/异常/边界），禅道中可自定义类型映射
- 关键词列：填 method（等价类/边界值/场景法/判定表）——显式测试方法论标注
- 步骤列：多步骤用换行 "1. …\\n2. …"
- 所属模块列：用 "父模块/子模块" 路径

本迭代只保证列结构与内容符合禅道导入模板，实际导入实测留迭代 2。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Union

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

from ..core.schema import TestCase

# 禅道导入模板固定表头（列顺序不得变更）
ZENTAO_HEADERS = ["所属模块", "用例标题", "前置条件", "步骤", "预期结果", "优先级", "用例类型", "关键词"]

# 优先级映射：P0→1、P1→2、P2→3、P3→4
PRIORITY_MAP = {"P0": 1, "P1": 2, "P2": 3, "P3": 4}

# Sheet 名（禅道导入识别名）
SHEET_NAME = "用例"

# 衍生 / 假设类用例的关键词标注后缀（迭代 9 / UP-2）：「场景法,假设」逗号并列，
# Excel 8 列模板不变，标注只出现在「关键词」列
ASSUMPTION_TAG = "假设"


def keyword_value(method: str, assumption: bool = False) -> str:
    """关键词列取值：方法论 + 可选「,假设」标注（如「场景法,假设」）。"""
    return f"{method},{ASSUMPTION_TAG}" if assumption else method


def split_keyword(value: str) -> tuple[str, bool]:
    """关键词列取值反解析：（方法论, 是否假设标注）。"""
    text = (value or "").strip()
    if text.endswith(f",{ASSUMPTION_TAG}"):
        return text[: -len(ASSUMPTION_TAG) - 1].strip(), True
    return text, False

# 各列宽度（便于人工审阅）
_COLUMN_WIDTHS = [24, 44, 28, 48, 44, 8, 10, 10]


def _write_header(ws: Worksheet) -> None:
    """写入表头行（加粗）。"""
    ws.append(ZENTAO_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center")


def _case_to_row(case: TestCase) -> list:
    """把 TestCase 映射为禅道一行数据（关键词列带「,假设」标注，迭代 9 / UP-2）。"""
    return [
        case.module,
        case.title,
        case.precondition,
        case.steps,
        case.expected,
        PRIORITY_MAP[case.priority],
        case.category,
        keyword_value(case.method, case.assumption),
    ]


def export_zentao_excel(
    cases: Iterable[TestCase], output_path: Union[str, Path]
) -> Path:
    """把用例集导出为禅道格式 xlsx，返回输出文件路径。"""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    _write_header(ws)

    count = 0
    for case in cases:
        ws.append(_case_to_row(case))
        count += 1

    # 便于人工审阅的轻量排版：冻结表头 + 列宽
    ws.freeze_panes = "A2"
    for idx, width in enumerate(_COLUMN_WIDTHS, 1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width

    wb.save(out)
    return out
