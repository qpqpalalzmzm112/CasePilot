"""生成编排：输入文档 → 解析 → LLM 网关生成 → pydantic 校验 → 质量门禁 → GenerationResult。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from ..adapters import openapi as openapi_adapter
from ..adapters import prd as prd_adapter
from .llm import LLMGateway
from .quality import check_coverage, format_coverage_report
from .schema import DocumentProfile, GenerationResult, LLMMode, SourceType, validate_cases

logger = logging.getLogger("casepilot")

# 输入格式自动识别：PRD 文本 vs OpenAPI 文档
PRD_SUFFIXES = {".md", ".txt"}
OPENAPI_SUFFIXES = {".yaml", ".yml", ".json"}


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

    return GenerationResult(
        source=str(p),
        cases=cases,
        mode=gateway.mode,
        generated_at=datetime.now(),
    )
