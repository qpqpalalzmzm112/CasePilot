"""生成编排：输入文档 → 解析 → LLM 网关生成 → pydantic 校验 → 质量门禁 → GenerationResult。

迭代 6 新增需求对账机制（UP-1）：生成后把用例逐条对账到 FR 清单——
每个 FR 至少 1 条用例（覆盖用例标题带 [FR-x.y] 追溯前缀），缺漏的 FR 自动生成
基础用例补齐（有效类 + 异常类各 1 条，method 按 FR 内容选方法论），
并输出中文对账报告（随生成流程打印，落盘 output/<输入名>_fr_coverage.md）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from ..adapters import openapi as openapi_adapter
from ..adapters import prd as prd_adapter
from . import mock_engine
from .llm import LLMGateway
from .quality import check_clarity, check_coverage, format_clarity_report, format_coverage_report
from .schema import (
    DocumentProfile,
    FRItem,
    GenerationResult,
    LLMMode,
    SourceType,
    TestCase,
    validate_cases,
)

logger = logging.getLogger("casepilot")

# 输入格式自动识别：PRD 文本 vs OpenAPI 文档
PRD_SUFFIXES = {".md", ".txt"}
OPENAPI_SUFFIXES = {".yaml", ".yml", ".json"}

# FR 对账报告落盘目录
FR_REPORT_DIR = "output"


def detect_source_type(path: Union[str, Path]) -> SourceType:
    """按扩展名自动识别输入类型：.md/.txt → PRD；.yaml/.yml/.json → OpenAPI。"""
    suffix = Path(path).suffix.lower()
    if suffix in PRD_SUFFIXES:
        return "prd"
    if suffix in OPENAPI_SUFFIXES:
        return "openapi"
    raise ValueError(
        f"暂不支持的输入格式：{suffix or '（无扩展名）'}，"
        "当前支持 .md/.txt（PRD）与 .yaml/.yml/.json（OpenAPI）"
    )


def parse_document(path: Union[str, Path], source_type: Optional[SourceType] = None) -> DocumentProfile:
    """解析输入文档为中间表示 DocumentProfile。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"输入文件不存在：{p}")
    kind = source_type or detect_source_type(p)
    if kind == "prd":
        return prd_adapter.parse_text(p.read_text(encoding="utf-8"), source=str(p))
    return openapi_adapter.parse_file(p)


# ---------------------------------------------------------------------------
# FR 需求对账（迭代 6 / UP-1）：逐条覆盖核对 + 缺漏自动补齐 + 中文对账报告
# ---------------------------------------------------------------------------

@dataclass
class FRCoverageItem:
    """单条 FR 的覆盖核对结果。"""

    fr_id: str
    summary: str
    count: int  # 覆盖用例数（补齐后含自动补齐用例）
    filled: bool  # True=规则生成未命中，已自动补齐


@dataclass
class FRCoverageReport:
    """FR 对账报告（确定性输出，不含时间戳）。"""

    source: str
    items: list[FRCoverageItem] = field(default_factory=list)
    case_count: int = 0  # 对账 + 补齐后的用例总数
    added_cases: int = 0  # 自动补齐新增的用例数

    @property
    def total(self) -> int:
        """FR 条目总数。"""
        return len(self.items)

    @property
    def filled_count(self) -> int:
        """被自动补齐的 FR 条数。"""
        return sum(1 for item in self.items if item.filled)

    def summary_line(self) -> str:
        """覆盖率总结句（对账后每个 FR 均 ≥1 条用例）。"""
        if self.filled_count:
            return (
                f"FR 覆盖率 100%（{self.total}/{self.total}），"
                f"其中自动补齐 {self.filled_count} 条 FR（新增 {self.added_cases} 条用例）"
            )
        return (
            f"FR 覆盖率 100%（{self.total}/{self.total}），"
            "全部 FR 由规则生成用例覆盖，无需自动补齐"
        )


def _fr_summary(text: str, limit: int = 24) -> str:
    """FR 需求摘要：确定性截断长文本，保证对账表可读。"""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _case_covers_fr(case: TestCase, fr: FRItem) -> bool:
    """用例覆盖 FR 的判定：标题带 [FR-x] 追溯前缀，或正文引用了需求条目原文。"""
    if fr.tag in case.title:
        return True
    blob = f"{case.title}\n{case.precondition}\n{case.steps}\n{case.expected}"
    return fr.text in blob


def _fr_backfill_cases(fr: FRItem, module: str) -> list[TestCase]:
    """缺漏 FR 的基础用例补齐：有效类 + 异常类各 1 条，method 按 FR 内容选方法论。

    FR 含安全 / 性能类需求词时标注「安全测试 / 性能测试」，其余用等价类；
    预期结果引用需求摘要并给出明确判定标准（不含模糊词）。
    """
    method = mock_engine.classify_requirement(fr.text, fr.section) or "等价类"
    summary = _fr_summary(fr.text)
    return [
        TestCase(
            title=f"{fr.tag} 【{method}】{summary} - 有效类验证（自动补齐）",
            precondition=f"已满足「{summary}」的基本使用条件，测试数据已准备就绪",
            steps=(
                f"1. 准备满足「{summary}」的合法输入数据\n"
                "2. 执行对应操作\n"
                "3. 观察系统响应与最终状态"
            ),
            expected=(
                f"系统成功处理「{summary}」的有效输入：处理成功，"
                f"返回结果与「{summary}」的需求定义一致，数据正确生效"
            ),
            priority="P1",
            category="功能",
            method=method,
            module=f"{module}/功能验证",
        ),
        TestCase(
            title=f"{fr.tag} 【{method}】{summary} - 异常类验证（自动补齐）",
            precondition=f"已满足「{summary}」的基本使用条件，测试数据已准备就绪",
            steps=(
                f"1. 准备违反「{summary}」的非法输入数据\n"
                "2. 执行对应操作\n"
                "3. 观察系统响应与校验提示"
            ),
            expected=(
                f"系统拒绝「{summary}」的无效输入：校验不通过并给出明确中文错误提示，"
                "数据不被写入"
            ),
            priority="P2",
            category="异常",
            method=method,
            module=f"{module}/异常验证",
        ),
    ]


def reconcile_fr_coverage(
    profile: DocumentProfile, cases: list[TestCase]
) -> tuple[list[TestCase], Optional[FRCoverageReport]]:
    """生成后 FR 逐条对账：覆盖核对 + 追溯前缀 + 缺漏自动补齐，返回（用例集, 对账报告）。

    - 每个 FR 至少 1 条用例：已覆盖的 FR 给首条覆盖用例补 [FR-x.y] 追溯前缀；
    - 未命中的 FR 自动生成基础用例补齐（标记「已补齐」）；
    - 文档无 FR 清单（如 OpenAPI 输入）时原样返回，api 命令行为不变。
    """
    if not profile.frs:
        return list(cases), None
    work = list(cases)
    items: list[FRCoverageItem] = []
    added = 0
    for fr in profile.frs:
        covering = [i for i, c in enumerate(work) if _case_covers_fr(c, fr)]
        if covering:
            first = covering[0]
            if fr.tag not in work[first].title:
                work[first] = work[first].model_copy(
                    update={"title": f"{fr.tag} {work[first].title}"}
                )
            items.append(
                FRCoverageItem(fr_id=fr.id, summary=_fr_summary(fr.text), count=len(covering), filled=False)
            )
        else:
            backfill = _fr_backfill_cases(fr, profile.module)
            work.extend(backfill)
            added += len(backfill)
            items.append(
                FRCoverageItem(fr_id=fr.id, summary=_fr_summary(fr.text), count=len(backfill), filled=True)
            )
    return work, FRCoverageReport(
        source=profile.source, items=items, case_count=len(work), added_cases=added
    )


def format_fr_coverage_report(report: FRCoverageReport) -> str:
    """把 FR 对账结果渲染为全中文 Markdown 报告（确定性，不含时间戳）。"""
    lines = [
        f"# FR 需求对账报告：{Path(report.source).name}",
        "",
        f"- 输入文档：{report.source}",
        f"- FR 条目：{report.total} 条；对账后用例：{report.case_count} 条",
        "",
        "## FR 覆盖清单",
        "",
        "| FR 编号 | 需求摘要 | 覆盖用例数 | 状态 |",
        "| --- | --- | --- | --- |",
    ]
    for item in report.items:
        status = "已补齐" if item.filled else "✓"
        lines.append(f"| FR-{item.fr_id} | {item.summary} | {item.count} | {status} |")
    lines.append("")
    lines.append(report.summary_line())
    return "\n".join(lines)


def generate_from_file(
    path: Union[str, Path], mode: Optional[LLMMode] = None
) -> GenerationResult:
    """端到端生成：文件 → 用例集（全部经 pydantic 校验）。"""
    p = Path(path)
    logger.info("正在解析输入文档：%s", p)
    profile = parse_document(p)
    logger.info(
        "解析完成：模块「%s」，功能点 / 接口 %d 个，数字边界 %d 个，主流程步骤 %d 步",
        profile.module, len(profile.features), len(profile.boundaries), len(profile.flows),
    )

    gateway = LLMGateway(mode=mode)
    raw_cases = gateway.generate_raw_cases(profile)
    cases = validate_cases(raw_cases)  # 结构合法率校验，失败抛清晰错误
    logger.info("已生成 %d 条用例（全部通过 pydantic 结构校验）", len(cases))

    # 质量门禁雏形：覆盖维度检查自动执行并打印中文结果，不达标输出中文警告与差距明细
    coverage = check_coverage(cases)
    log = logger.warning if not coverage.ok else logger.info
    for line in format_coverage_report(coverage).splitlines():
        log(line)

    # 质量门禁（迭代 6）：表述清晰度检查（预期结果模糊词禁令），命中输出中文警告明细
    clarity = check_clarity(cases)
    log = logger.warning if not clarity.ok else logger.info
    for line in format_clarity_report(clarity).splitlines():
        log(line)

    # FR 需求对账（迭代 6 / UP-1）：逐条覆盖核对 + 缺漏自动补齐 + 报告打印落盘
    cases, fr_report = reconcile_fr_coverage(profile, cases)
    if fr_report is not None:
        text = format_fr_coverage_report(fr_report)
        for line in text.splitlines():
            logger.info(line)
        out_dir = Path(FR_REPORT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / f"{p.stem}_fr_coverage.md"
        report_path.write_text(text, encoding="utf-8")
        logger.info("FR 对账报告已落盘：%s", report_path)

    return GenerationResult(
        source=str(p),
        cases=cases,
        mode=gateway.mode,
        generated_at=datetime.now(),
    )
