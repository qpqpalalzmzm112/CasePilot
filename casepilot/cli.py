"""CasePilot 命令行入口（typer）。

本迭代仅提供：
- casepilot gen -i <文件> [--format xlsx] [-o 输出路径]：生成禅道 Excel 用例
- casepilot --version：显示版本号

运行时打印中文进度与当前 LLM 模式；--help 全中文。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from typer.core import TyperCommand, TyperGroup

from . import __version__
from .core.generator import generate_from_file
from .core.llm import MODE_DESCRIPTIONS
from .exporters.zentao_excel import export_zentao_excel


class _ChineseHelpMixin:
    """把内置 --help 选项文案改为中文（typer 未提供帮助文案本地化参数）。"""

    def get_help_option(self, ctx):
        option = super().get_help_option(ctx)
        if option is not None:
            option.help = "显示帮助信息并退出"
        return option


class _ChineseHelpCommand(_ChineseHelpMixin, TyperCommand):
    """中文帮助文案的子命令基类。"""


class _ChineseHelpGroup(_ChineseHelpMixin, TyperGroup):
    """中文帮助文案的命令组基类。"""

# 中文日志配置（进度信息走 stdlib logging）
logger = logging.getLogger("casepilot")
logger.setLevel(logging.INFO)


def _ensure_log_handler() -> None:
    """命令运行时再挂 handler：此时 sys.stderr 已是 CLI 重定向后的流。"""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

app = typer.Typer(
    cls=_ChineseHelpGroup,
    help="CasePilot：中文需求文档 / OpenAPI 文档 → 结构化测试用例 → 禅道 Excel",
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

# 本迭代唯一支持的导出格式
_SUPPORTED_FORMATS = ("xlsx",)


def _version_callback(value: bool) -> None:
    """--version 回调：打印版本号并退出。"""
    if value:
        typer.echo(f"casepilot {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="显示版本号并退出",
    ),
) -> None:
    """CasePilot：按测试方法论（等价类 / 边界值 / 场景法 / 判定表）生成结构化测试用例，导出禅道 Excel。"""


@app.command("gen", cls=_ChineseHelpCommand)
def gen(
    input: Path = typer.Option(
        ...,
        "-i",
        "--input",
        help="输入文档路径：.md/.txt（中文 PRD）或 .yaml/.yml/.json（OpenAPI），自动识别",
    ),
    fmt: str = typer.Option(
        "xlsx",
        "--format",
        help="导出格式（当前仅支持 xlsx，即禅道导入用例表）",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "-o",
        "--output",
        help="输出文件路径，默认 output/<输入名>_cases.xlsx",
    ),
) -> None:
    """解析需求文档并生成禅道格式测试用例 Excel。"""
    _ensure_log_handler()

    # 格式校验（本迭代仅支持 xlsx）
    if fmt not in _SUPPORTED_FORMATS:
        typer.echo(f"错误：暂不支持导出格式「{fmt}」，当前仅支持 xlsx", err=True)
        raise typer.Exit(code=1)

    # 输出路径默认值：output/<输入名>_cases.xlsx
    out_path = output or Path("output") / f"{input.stem}_cases.xlsx"

    try:
        result = generate_from_file(input)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"当前 LLM 模式：{MODE_DESCRIPTIONS[result.mode]}")
    logger.info("共生成 %d 条用例，正在导出禅道 Excel…", len(result.cases))

    saved = export_zentao_excel(result.cases, out_path)
    typer.echo(f"导出完成：{saved}（共 {len(result.cases)} 条用例）")


if __name__ == "__main__":
    app()
