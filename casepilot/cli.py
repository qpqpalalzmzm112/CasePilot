"""CasePilot 命令行入口（typer）。

本迭代提供：
- casepilot gen -i <文件> [--emit xlsx|pytest|md] [-o 输出路径]：PRD / OpenAPI 通用生成入口
- casepilot api -i <openapi> [--emit xlsx|pytest|md] [-o 输出路径]：OpenAPI 专用生成入口
- casepilot eval -i <文件> [--golden <golden.json>]：评测生成质量（4 维评分，报告落盘 output/）
- casepilot judge <cases.xlsx> [--source <prd.md>] [--strict] [-o 报告.md]：LLM-as-a-Judge 用例质量自评（双模式，--source 启用幻觉溯源校验，--strict 删除推测项用例）
- casepilot --version：显示版本号

导出格式：
- xlsx：禅道导入用例表（默认，导出后自动执行禅道导入前校验并输出中文报告）
- pytest：pytest 脚本骨架（可被 pytest 正常收集）
- md：Markdown 用例表（按模块分组 + 方法论统计）

运行时打印中文进度、覆盖维度检查结果与当前 LLM 模式；--help 全中文。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from typer.core import TyperCommand, TyperGroup

from evals.judge import judge_cases, load_cases_from_excel, run_grounding_check
from evals.run import run_eval

from . import __version__
from .core.generator import detect_source_type, generate_from_file
from .core.llm import MODE_DESCRIPTIONS
from .core.quality import KIND_MEASUREMENT, validate_zentao_excel
from .exporters.markdown_export import export_markdown
from .exporters.pytest_skeleton import export_pytest_skeleton
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
    help="CasePilot：中文需求文档 / OpenAPI 文档 → 结构化测试用例 → 禅道 Excel / pytest 骨架 / Markdown",
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

# 支持的导出格式（xlsx 为默认，向后兼容迭代 1 行为）
SUPPORTED_EMITS = ("xlsx", "pytest", "md")

# 各导出格式的默认输出文件名后缀
_DEFAULT_SUFFIXES = {
    "xlsx": "_cases.xlsx",
    "pytest": "_test_skeleton.py",
    "md": "_cases.md",
}

_EMIT_HELP = "导出格式：xlsx（禅道 Excel，默认）/ pytest（pytest 脚本骨架）/ md（Markdown 用例表）"


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
    """CasePilot：按测试方法论（等价类 / 边界值 / 场景法 / 判定表）生成结构化测试用例。"""


def _resolve_output(input_path: Path, emit: str, output: Optional[Path]) -> Path:
    """输出路径默认值：output/<输入名><格式后缀>。"""
    if output is not None:
        return output
    return Path("output") / f"{input_path.stem}{_DEFAULT_SUFFIXES[emit]}"


def _run_generate(input_path: Path, emit: str, output: Optional[Path], openapi_only: bool) -> None:
    """生成 + 按格式导出的公共编排（gen / api 共用）。"""
    _ensure_log_handler()

    if emit not in SUPPORTED_EMITS:
        typer.echo(f"错误：暂不支持导出格式「{emit}」，支持 {' / '.join(SUPPORTED_EMITS)}", err=True)
        raise typer.Exit(code=1)

    if openapi_only:
        try:
            if detect_source_type(input_path) != "openapi":
                typer.echo(
                    f"错误：api 命令仅支持 OpenAPI 接口文档（.yaml/.yml/.json），"
                    f"PRD 文档请使用 casepilot gen",
                    err=True,
                )
                raise typer.Exit(code=1)
        except ValueError:
            pass  # 无法识别的扩展名交由后续生成流程报错（保持迭代 1 错误文案）

    out_path = _resolve_output(input_path, emit, output)

    try:
        result = generate_from_file(input_path)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"当前 LLM 模式：{MODE_DESCRIPTIONS[result.mode]}")
    logger.info("共生成 %d 条用例，正在导出 %s 格式…", len(result.cases), emit)

    if emit == "xlsx":
        saved = export_zentao_excel(result.cases, out_path)
        typer.echo(f"导出完成：{saved}（共 {len(result.cases)} 条用例）")
        # 导入前校验闭环：导出后立即对照禅道官方模板校验并输出中文报告
        report = validate_zentao_excel(saved)
        typer.echo(report.format_report())
    elif emit == "pytest":
        saved = export_pytest_skeleton(result.cases, out_path, source=str(input_path))
        typer.echo(f"导出完成：{saved}（共 {len(result.cases)} 条用例）")
    else:
        saved = export_markdown(result.cases, out_path, source=str(input_path))
        typer.echo(f"导出完成：{saved}（共 {len(result.cases)} 条用例）")

    # FR 对账核对表摘要（迭代 8 / UP-1）：CLI 结束打印覆盖率总结句
    if result.fr_coverage_summary:
        typer.echo(f"FR 对账摘要：{result.fr_coverage_summary}")


@app.command("gen", cls=_ChineseHelpCommand)
def gen(
    input: Path = typer.Option(
        ...,
        "-i",
        "--input",
        help="输入文档路径：.md/.txt（中文 PRD）或 .yaml/.yml/.json（OpenAPI），自动识别",
    ),
    emit: str = typer.Option(
        "xlsx",
        "--emit",
        "--format",
        help=_EMIT_HELP,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "-o",
        "--output",
        help="输出文件路径，默认 output/<输入名>_cases.xlsx（pytest/md 格式见 --help）",
    ),
) -> None:
    """解析需求文档并生成测试用例（禅道 Excel / pytest 骨架 / Markdown）。"""
    _run_generate(input, emit, output, openapi_only=False)


@app.command("api", cls=_ChineseHelpCommand)
def api(
    input: Path = typer.Option(
        ...,
        "-i",
        "--input",
        help="OpenAPI 文档路径：.yaml/.yml/.json",
    ),
    emit: str = typer.Option(
        "xlsx",
        "--emit",
        "--format",
        help=_EMIT_HELP,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "-o",
        "--output",
        help="输出文件路径，默认 output/<输入名>_cases.xlsx（pytest/md 格式见 --help）",
    ),
) -> None:
    """解析 OpenAPI 接口文档并生成测试用例（接口专用入口，支持 --emit pytest 骨架）。"""
    _run_generate(input, emit, output, openapi_only=True)


@app.command("eval", cls=_ChineseHelpCommand)
def eval_cmd(
    input: Path = typer.Option(
        ...,
        "-i",
        "--input",
        help="输入文档路径：.md/.txt（中文 PRD）或 .yaml/.yml/.json（OpenAPI），自动识别",
    ),
    golden: Optional[Path] = typer.Option(
        None,
        "--golden",
        help="golden 基准集 JSON 路径，默认自动匹配 evals/datasets/<输入名>_golden.json",
    ),
) -> None:
    """评测生成质量：对照 golden 基准集输出 4 维中文评分报告（落盘 output/eval_report_<样例名>.md）。"""
    _ensure_log_handler()
    try:
        text, report_path = run_eval(input, golden)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(text)
    typer.echo(f"\n评分报告已保存：{report_path}")


@app.command("judge", cls=_ChineseHelpCommand)
def judge(
    cases: Path = typer.Argument(
        ...,
        help="禅道格式用例 Excel 路径（gen 产出的 *_cases.xlsx）",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "-o",
        "--output",
        help="评审报告 Markdown 输出路径（可选，不指定时仅打印到终端）",
    ),
    source: Optional[Path] = typer.Option(
        None,
        "--source",
        help="PRD 原文路径：启用幻觉溯源校验（步骤与预期须有 PRD 依据），无依据断言标记为「推测项」",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="严格模式（需与 --source 联用）：直接删除推测项用例并输出中文告警",
    ),
) -> None:
    """LLM-as-a-Judge 用例质量自评（双模式：LLM 评审 / 规则评分降级；--source 可选溯源校验）。"""
    _ensure_log_handler()
    if strict and source is None:
        typer.echo("错误：--strict 需与 --source 联用（先指定溯源依据 PRD 原文）", err=True)
        raise typer.Exit(code=1)
    try:
        rows = load_cases_from_excel(cases)
        grounding = None
        if source is not None:
            rows, grounding = run_grounding_check(rows, source, strict=strict)
            for item in grounding.removed:
                # 测量值预言按禁令拦截（迭代 8 / UP-2），不与 [假设] 推测项混同
                label = (
                    "测量值预言用例" if item.kind == KIND_MEASUREMENT else "推测项用例"
                )
                typer.echo(
                    f"严格模式告警：已删除{label}「{item.title}」（{item.kind}）：片段「{item.fragment}」",
                    err=True,
                )
        report = judge_cases(rows, grounding=grounding)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    text = report.format_report(str(cases))
    typer.echo(text)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        typer.echo(f"\n评审报告已保存：{output}")


if __name__ == "__main__":
    app()
