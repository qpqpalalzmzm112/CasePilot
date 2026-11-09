"""Markdown 用例导出（--emit md）。

输出规范：
- `# 用例集：<来源>` 文件标题
- `## <模块>` 按所属模块分组
- 每条用例一个三级标题 + 8 要素表格
  （用例标题 / 前置条件 / 步骤 / 预期结果 / 优先级 / 用例类型 / 测试方法论 / 所属模块）
- 尾部附方法论统计表（每种方法论用例数与占比）——显式测试方法论标注
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Union

from ..core.schema import METHODS, TestCase

# 8 要素行名（顺序固定）
ELEMENT_ROWS = (
    "用例标题",
    "前置条件",
    "步骤",
    "预期结果",
    "优先级",
    "用例类型",
    "测试方法论",
    "所属模块",
)


def _escape(text: str) -> str:
    """转义 Markdown 表格单元格：竖线转义、换行转 <br>。"""
    return text.replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")


def build_markdown(cases: Iterable[TestCase], source: str = "") -> str:
    """把用例集渲染为 Markdown 文本。"""
    case_list = list(cases)
    lines: list[str] = [
        f"# 用例集：{source or '（未提供）'}",
        "",
        f"> 由 CasePilot 生成，共 {len(case_list)} 条用例；每条用例含 8 要素与显式测试方法论标注。",
        "",
    ]

    # 按所属模块分组（保持首次出现顺序）
    modules: list[str] = []
    grouped: dict[str, list[TestCase]] = {}
    for case in case_list:
        grouped.setdefault(case.module, []).append(case)
        if case.module not in modules:
            modules.append(case.module)

    for module in modules:
        lines.append(f"## {module}")
        lines.append("")
        for case in grouped[module]:
            lines.append(f"### {case.title}")
            lines.append("")
            lines.append("| 要素 | 内容 |")
            lines.append("| --- | --- |")
            values = (
                case.title,
                case.precondition,
                case.steps,
                case.expected,
                case.priority,
                case.category,
                f"{case.method}{'（设计假设）' if case.assumption else ''}",
                case.module,
            )
            for name, value in zip(ELEMENT_ROWS, values):
                lines.append(f"| {name} | {_escape(value)} |")
            lines.append("")

    # 尾部方法论统计表
    lines.append("## 方法论统计")
    lines.append("")
    lines.append("| 方法论 | 用例数 | 占比 |")
    lines.append("| --- | --- | --- |")
    total = len(case_list) or 1
    for method in METHODS:
        count = sum(1 for c in case_list if c.method == method)
        lines.append(f"| {method} | {count} | {count / total * 100:.1f}% |")
    lines.append(f"| 合计 | {len(case_list)} | 100.0% |")
    lines.append("")
    return "\n".join(lines)


def export_markdown(
    cases: Iterable[TestCase], output_path: Union[str, Path], source: str = ""
) -> Path:
    """把用例集导出为 Markdown 用例表 .md 文件，返回输出路径。"""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_markdown(cases, source=source), encoding="utf-8")
    return out
