"""质量门禁雏形：覆盖维度检查 + 表述清晰度检查 + 禅道导入前校验器（全中文输出）。

三部分能力：
1. 覆盖维度检查（迭代 2）：每文档 ≥8 条用例、method ≥3 种、category ≥3 种、
   method 无空值，不达标时输出中文差距明细；由生成流程自动执行。
2. 表述清晰度检查（迭代 6 / UP-2）：预期结果模糊词禁令——expected 不得包含
   「或 / 可能 / 视情况」类模糊表述，命中输出中文警告明细（用例标题 + 命中词）。
   模糊词词表 FUZZY_WORDS 是全项目唯一事实来源，evals/judge.py 复用同一词表。
3. 禅道导入前校验器（迭代 2）：对照禅道官方用例导入模板逐字段检查 Excel，
   输出中文校验报告（可导入 / 不可导入 + 错误字段明细）。

全部输出为中文；本模块不做任何网络访问。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Union

from openpyxl import load_workbook

from ..exporters.zentao_excel import ZENTAO_HEADERS
from .schema import CATEGORIES, METHODS, TestCase

# 覆盖维度门禁下限（与 mock 引擎保持一致）
MIN_CASES = 8
MIN_METHOD_COVERAGE = 3
MIN_CATEGORY_COVERAGE = 3

# 禅道导入模板必填列（列名与 ZENTAO_HEADERS 一致）
REQUIRED_COLUMNS = ("所属模块", "用例标题", "步骤", "预期结果", "优先级")
# 仅建议填写、缺失不阻断导入的列
OPTIONAL_COLUMNS = ("前置条件", "用例类型", "关键词")

# 预期结果模糊词禁令（迭代 6 / UP-2）：expected 出现以下词即视为判定标准不明确。
# 每词禁用原因：
# - 或：给出两个可接受结果，执行后无法判定唯一通过 / 失败
# - 可能：结果不确定，断言无法写死
# - 视情况：把判定推给执行者主观裁量，不可自动校验
# - 大概：近似表述，无明确阈值或状态可校验
# - 也许：同「可能」，结果不确定
# - 正常情况：限定词含糊，未定义何为「正常」，异常分支判定缺失
# - 若干：数量不确定，无法准备精确测试数据
# - 等等：列举未穷尽，检查范围不明确
# - 之类：类别未穷尽，检查范围不明确
# - 应该可以：推测性结论，不是可校验的判定标准
# - 尽量：目标非强制，达成与否都可解释为通过
FUZZY_WORDS: tuple[str, ...] = (
    "或",
    "可能",
    "视情况",
    "大概",
    "也许",
    "正常情况",
    "若干",
    "等等",
    "之类",
    "应该可以",
    "尽量",
)

# 禅道数字优先级合法取值（P0→1、P1→2、P2→3、P3→4）
VALID_PRIORITY_VALUES = ("1", "2", "3", "4")

# 期望的 Sheet 名（禅道导入识别名）
EXPECTED_SHEET_NAME = "用例"


@dataclass
class CoverageReport:
    """覆盖维度检查结果（中文差距明细）。"""

    total: int
    methods: tuple[str, ...]
    categories: tuple[str, ...]
    empty_method_count: int
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """全部维度达标时为 True。"""
        return not self.issues


def _field(item: Union[TestCase, Mapping[str, Any]], name: str) -> str:
    """统一读取字段：支持 TestCase 模型与原始 dict 两种输入。"""
    if isinstance(item, Mapping):
        value = item.get(name, "")
    else:
        value = getattr(item, name, "")
    return str(value).strip() if value is not None else ""


def check_coverage(cases: Iterable[Union[TestCase, Mapping[str, Any]]]) -> CoverageReport:
    """覆盖维度检查：用例数量、方法论覆盖、类型覆盖、方法论标注非空。

    接受 pydantic 校验后的 TestCase，或生成中间态的原始 dict（便于门禁前置检查）。
    """
    items = list(cases)
    methods = [_field(c, "method") for c in items]
    categories = [_field(c, "category") for c in items]
    used_methods = tuple(m for m in dict.fromkeys(methods) if m)
    used_categories = tuple(c for c in dict.fromkeys(categories) if c)
    empty_method_count = sum(1 for m in methods if not m)

    issues: list[str] = []
    if len(items) < MIN_CASES:
        issues.append(
            f"用例数量 {len(items)} 条，低于下限 {MIN_CASES} 条，还差 {MIN_CASES - len(items)} 条"
        )
    if len(used_methods) < MIN_METHOD_COVERAGE:
        missing = [m for m in METHODS if m not in used_methods]
        issues.append(
            f"方法论覆盖仅 {len(used_methods)} 种（{'、'.join(used_methods) or '无'}），"
            f"低于下限 {MIN_METHOD_COVERAGE} 种，缺少：{'、'.join(missing) or '—'}"
        )
    if len(used_categories) < MIN_CATEGORY_COVERAGE:
        missing = [c for c in CATEGORIES if c not in used_categories]
        issues.append(
            f"用例类型覆盖仅 {len(used_categories)} 种（{'、'.join(used_categories) or '无'}），"
            f"低于下限 {MIN_CATEGORY_COVERAGE} 种，缺少：{'、'.join(missing) or '—'}"
        )
    if empty_method_count:
        issues.append(
            f"方法论标注存在空值：{empty_method_count} 条用例的 method 为空，"
            "请补充显式方法论标注（等价类 / 边界值 / 场景法 / 判定表 / 安全测试 / 性能测试）"
        )

    return CoverageReport(
        total=len(items),
        methods=used_methods,
        categories=used_categories,
        empty_method_count=empty_method_count,
        issues=issues,
    )


def format_coverage_report(report: CoverageReport) -> str:
    """把覆盖维度检查结果渲染为全中文报告（多行文本）。"""
    lines = [f"覆盖维度检查：共 {report.total} 条用例"]
    lines.append(
        f"  - 用例数量 {report.total} 条，下限 {MIN_CASES} 条"
        + ("（达标）" if report.total >= MIN_CASES else "（未达标）")
    )
    lines.append(
        f"  - 方法论覆盖 {len(report.methods)} 种"
        f"（{'、'.join(report.methods) or '无'}），下限 {MIN_METHOD_COVERAGE} 种"
        + ("（达标）" if len(report.methods) >= MIN_METHOD_COVERAGE else "（未达标）")
    )
    lines.append(
        f"  - 用例类型覆盖 {len(report.categories)} 种"
        f"（{'、'.join(report.categories) or '无'}），下限 {MIN_CATEGORY_COVERAGE} 种"
        + ("（达标）" if len(report.categories) >= MIN_CATEGORY_COVERAGE else "（未达标）")
    )
    lines.append(
        f"  - 方法论标注非空：{report.total - report.empty_method_count} / {report.total} 条"
        + ("（达标）" if not report.empty_method_count else "（未达标）")
    )
    if report.ok:
        lines.append("覆盖维度检查结论：达标")
    else:
        lines.append("覆盖维度检查结论：未达标，差距明细如下：")
        lines.extend(f"  - {issue}" for issue in report.issues)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 表述清晰度检查（迭代 6 / UP-2）：预期结果模糊词禁令
# ---------------------------------------------------------------------------

@dataclass
class ClarityReport:
    """表述清晰度检查结果（模糊词命中中文警告明细）。"""

    total: int
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """预期结果全部无模糊词时为 True。"""
        return not self.issues


def find_fuzzy_words(text: str) -> list[str]:
    """返回文本命中的模糊词（保持 FUZZY_WORDS 词表顺序，去重）。"""
    return [w for w in FUZZY_WORDS if w in text]


def check_clarity(cases: Iterable[Union[TestCase, Mapping[str, Any]]]) -> ClarityReport:
    """表述清晰度检查：expected 不得包含模糊词，命中输出中文警告明细（用例标题 + 命中词）。

    接受 pydantic 校验后的 TestCase，或生成中间态的原始 dict。
    """
    items = list(cases)
    issues: list[str] = []
    for case in items:
        title = _field(case, "title") or "（无标题）"
        fuzzy = find_fuzzy_words(_field(case, "expected"))
        if fuzzy:
            issues.append(
                f"用例「{title}」预期结果含模糊词「{'、'.join(fuzzy)}」，"
                "请改为明确的判定标准（具体状态 / 提示语 / 数值）"
            )
    return ClarityReport(total=len(items), issues=issues)


def format_clarity_report(report: ClarityReport) -> str:
    """把表述清晰度检查结果渲染为全中文报告（多行文本）。"""
    lines = [f"表述清晰度检查：共 {report.total} 条用例（预期结果模糊词禁令）"]
    if report.ok:
        lines.append("  - 预期结果模糊词命中 0 条（达标）")
        lines.append("表述清晰度检查结论：达标")
    else:
        lines.append(f"  - 预期结果模糊词命中 {len(report.issues)} 条，警告明细如下：")
        lines.extend(f"  - {issue}" for issue in report.issues)
        lines.append("表述清晰度检查结论：存在模糊表述，请按明细修正后重新生成")
    return "\n".join(lines)


@dataclass
class ZentaoImportReport:
    """禅道导入前校验结果（中文报告）。"""

    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_rows: int = 0

    @property
    def ok(self) -> bool:
        """无错误即可导入（警告不阻断）。"""
        return not self.errors

    def format_report(self) -> str:
        """渲染为全中文校验报告。"""
        lines = [f"禅道导入前校验：{self.path}"]
        if self.ok and not self.warnings:
            lines.append(
                f"  - Sheet「{EXPECTED_SHEET_NAME}」存在，表头 {len(ZENTAO_HEADERS)} 列与官方导入模板一致"
            )
            lines.append(f"  - 数据行 {self.checked_rows} 行，必填字段完整")
            lines.append(f"  - 优先级取值全部合法（{'/'.join(VALID_PRIORITY_VALUES)}）")
        for err in self.errors:
            lines.append(f"  - 错误：{err}")
        for warn in self.warnings:
            lines.append(f"  - 警告：{warn}")
        if self.ok:
            lines.append("校验结论：可导入")
        else:
            lines.append(f"校验结论：不可导入（共 {len(self.errors)} 处错误）")
        return "\n".join(lines)


def _is_nonempty(value: Any) -> bool:
    """单元格非空判断（数字 0 也算有效值）。"""
    if value is None:
        return False
    return str(value).strip() != ""


def _priority_ok(value: Any) -> bool:
    """优先级取值合法性：禅道数字优先级 1/2/3/4。"""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return float(value).is_integer() and str(int(value)) in VALID_PRIORITY_VALUES
    return str(value).strip() in VALID_PRIORITY_VALUES


def validate_zentao_excel(path: Union[str, Path]) -> ZentaoImportReport:
    """对照禅道官方用例导入模板逐字段检查 Excel，返回中文校验报告。

    检查项（错误阻断导入，警告仅提示）：
    - Sheet「用例」存在
    - 表头列名与顺序与官方模板逐列一致
    - 至少一行用例数据
    - 必填列（所属模块 / 用例标题 / 步骤 / 预期结果 / 优先级）非空
    - 优先级取值为 1/2/3/4
    - 用例类型 / 关键词取值不在枚举内时给出警告（禅道支持自定义映射，不阻断）
    """
    p = Path(path)
    report = ZentaoImportReport(path=p)

    if not p.is_file():
        report.errors.append(f"文件不存在：{p}")
        return report

    try:
        wb = load_workbook(p)
    except Exception as exc:  # noqa: BLE001 - 无法打开统一转中文错误
        report.errors.append(f"无法打开 Excel 文件（格式非法或已损坏）：{exc}")
        return report

    if EXPECTED_SHEET_NAME not in wb.sheetnames:
        report.errors.append(
            f"缺少「{EXPECTED_SHEET_NAME}」工作表（禅道导入模板 Sheet 名为「{EXPECTED_SHEET_NAME}」，"
            f"实际工作表：{'、'.join(wb.sheetnames) or '无'}）"
        )
        return report

    ws = wb[EXPECTED_SHEET_NAME]

    # 表头逐列比对
    header_problems = 0
    for idx, expected in enumerate(ZENTAO_HEADERS, 1):
        actual = ws.cell(row=1, column=idx).value
        actual_text = "" if actual is None else str(actual).strip()
        if actual_text != expected:
            header_problems += 1
            report.errors.append(
                f"第 1 行第 {idx} 列列名错误：期望「{expected}」，实际「{actual_text or '（空）'}」"
            )
    extra = ws.cell(row=1, column=len(ZENTAO_HEADERS) + 1).value
    if _is_nonempty(extra):
        report.errors.append(
            f"表头存在模板之外的多余列（第 {len(ZENTAO_HEADERS) + 1} 列「{str(extra).strip()}」）"
        )

    # 数据行检查
    data_rows = 0
    for row in range(2, ws.max_row + 1):
        values = [ws.cell(row=row, column=col).value for col in range(1, len(ZENTAO_HEADERS) + 1)]
        if not any(_is_nonempty(v) for v in values):
            continue  # 整行空白跳过
        data_rows += 1
        row_no = row
        # 必填列非空
        for col_idx in (1, 2, 4, 5, 6):  # 所属模块 / 用例标题 / 步骤 / 预期结果 / 优先级
            if not _is_nonempty(values[col_idx - 1]):
                report.errors.append(f"第 {row_no} 行「{ZENTAO_HEADERS[col_idx - 1]}」为空，禅道导入必填")
        # 优先级取值
        if _is_nonempty(values[5]) and not _priority_ok(values[5]):
            report.errors.append(
                f"第 {row_no} 行「优先级」非法取值「{str(values[5]).strip()}」"
                f"（合法取值：{'/'.join(VALID_PRIORITY_VALUES)}，对应 P0/P1/P2/P3）"
            )
        # 用例类型 / 关键词枚举警告（不阻断）
        if _is_nonempty(values[6]) and str(values[6]).strip() not in CATEGORIES:
            report.warnings.append(
                f"第 {row_no} 行「用例类型」取值「{str(values[6]).strip()}」不在常用枚举"
                f"（{'/'.join(CATEGORIES)}）内，禅道中需自定义类型映射"
            )
        if _is_nonempty(values[7]) and str(values[7]).strip() not in METHODS:
            report.warnings.append(
                f"第 {row_no} 行「关键词」取值「{str(values[7]).strip()}」不在方法论枚举"
                f"（{'/'.join(METHODS)}）内，方法论标注可能丢失"
            )

    report.checked_rows = data_rows
    if data_rows == 0 and not header_problems:
        report.errors.append("用例数据为空，无可导入内容")
    return report
