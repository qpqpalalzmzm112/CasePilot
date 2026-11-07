"""核心数据模型与结构化校验（pydantic v2）。

TestCase / GenerationResult 是全链路的唯一事实来源：
生成结果必须全部通过模型校验，非法字段（如优先级越界、方法论不在枚举内）
会被直接拒绝并抛出包含字段名与合法取值的清晰错误。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 枚举取值（全中文，与禅道工作流保持一致）
Priority = Literal["P0", "P1", "P2", "P3"]
Category = Literal["功能", "接口", "异常", "边界"]
Method = Literal["等价类", "边界值", "场景法", "判定表"]
LLMMode = Literal["openai", "ollama", "mock"]
SourceType = Literal["prd", "openapi"]

PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")
CATEGORIES: tuple[str, ...] = ("功能", "接口", "异常", "边界")
METHODS: tuple[str, ...] = ("等价类", "边界值", "场景法", "判定表")
LLM_MODES: tuple[str, ...] = ("openai", "ollama", "mock")


class TestCase(BaseModel):
    """单条结构化测试用例。"""

    # 禁止未知字段，保证结构合法率可被严格校验
    model_config = ConfigDict(extra="forbid")

    # 防止 pytest 按 Test* 规则误收集本数据模型
    __test__: ClassVar[bool] = False

    title: str = Field(..., min_length=1, description="用例标题")
    precondition: str = Field(..., min_length=1, description="前置条件")
    steps: str = Field(..., min_length=1, description="操作步骤，多步骤用换行 \"1. …\\n2. …\" 形式")
    expected: str = Field(..., min_length=1, description="预期结果")
    priority: Priority = Field(..., description="优先级：P0/P1/P2/P3")
    category: Category = Field(..., description="用例类型：功能/接口/异常/边界")
    method: Method = Field(..., description="测试方法论：等价类/边界值/场景法/判定表")
    module: str = Field(..., min_length=1, description="所属模块，父模块/子模块 路径")

    @field_validator("title", "precondition", "steps", "expected", "module")
    @classmethod
    def _strip_and_check(cls, v: str) -> str:
        """去除首尾空白，空白内容视为非法。"""
        v = v.strip()
        if not v:
            raise ValueError("字段内容不能为空")
        return v


class GenerationResult(BaseModel):
    """一次生成任务的完整结果。"""

    source: str = Field(..., min_length=1, description="输入来源（文件路径或文档名）")
    cases: list[TestCase] = Field(..., min_length=1, description="生成的测试用例集")
    mode: LLMMode = Field(..., description="LLM 网关模式：openai/ollama/mock")
    generated_at: datetime = Field(default_factory=datetime.now, description="生成时间")


class Feature(BaseModel):
    """文档解析出的功能点 / 接口。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="功能点或接口名称")
    description: str = Field("", description="补充描述")
    kind: Literal["function", "api"] = Field("function", description="function=功能点，api=接口")


class Boundary(BaseModel):
    """文档解析出的数字边界。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, description="边界出现的原文片段")
    value: float = Field(..., description="边界数值")
    kind: Literal["range_min", "range_max", "threshold"] = Field(
        "threshold", description="range_min/range_max=区间端点，threshold=阈值"
    )
    unit: str = Field("", description="数值单位（位/次/分钟/件 等）")


class DocumentProfile(BaseModel):
    """输入文档解析后的中间表示，供 mock 引擎与 LLM 提示词共用。"""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., min_length=1, description="来源文件名或路径")
    source_type: SourceType = Field(..., description="prd=需求文档，openapi=接口文档")
    module: str = Field(..., min_length=1, description="文档主模块名")
    features: list[Feature] = Field(default_factory=list, description="功能点 / 接口列表")
    boundaries: list[Boundary] = Field(default_factory=list, description="数字边界列表")
    flows: list[str] = Field(default_factory=list, description="主流程步骤列表")

    def summary_text(self) -> str:
        """输出文本摘要，供 LLM 提示词与日志使用。"""
        lines = [f"文档：{self.source}（类型：{self.source_type}，模块：{self.module}）"]
        if self.features:
            lines.append("功能点 / 接口：")
            lines.extend(f"- [{f.kind}] {f.name}：{f.description}" for f in self.features)
        if self.boundaries:
            lines.append("数字边界：")
            lines.extend(
                f"- {b.text}（数值 {b.value:g}{b.unit}，类型 {b.kind}）" for b in self.boundaries
            )
        if self.flows:
            lines.append("主流程：")
            lines.extend(f"{i}. {step}" for i, step in enumerate(self.flows, 1))
        return "\n".join(lines)


def validate_case(data: dict) -> TestCase:
    """把生成的原始 dict 校验为 TestCase，失败时抛出清晰的中文错误。"""
    try:
        return TestCase.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - 统一转成中文错误信息
        raise ValueError(f"测试用例结构校验失败：{exc}") from exc


def validate_cases(raw_cases: list[dict]) -> list[TestCase]:
    """批量校验，错误信息中带上用例序号，便于定位问题。"""
    cases: list[TestCase] = []
    for i, raw in enumerate(raw_cases, 1):
        try:
            cases.append(TestCase.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"第 {i} 条用例结构校验失败：{exc}") from exc
    return cases
