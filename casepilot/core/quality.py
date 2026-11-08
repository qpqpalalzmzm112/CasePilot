"""质量门禁雏形：覆盖维度检查 + 表述清晰度检查 + 幻觉溯源辅助 + 禅道导入前校验器（全中文输出）。

四部分能力：
1. 覆盖维度检查（迭代 2）：每文档 ≥8 条用例、method ≥3 种、category ≥3 种、
   method 无空值，不达标时输出中文差距明细；由生成流程自动执行。
2. 表述清晰度检查 / 模糊词门禁（迭代 6 警告 → 迭代 7 门禁拒绝）：expected 不得包含
   「或 / 可能 / 视情况」类模糊表述。FUZZY_WORDS 是全项目唯一事实来源，
   evals/judge.py 复用同一词表；check_clarity 输出中文警告明细，
   check_fuzzy_gate / drop_fuzzy_cases 是阻断接口（LLM 模式命中即校验失败、
   回灌重试，重试耗尽丢弃并告警）。
3. 幻觉溯源检查辅助（迭代 7 / UP-2）：用例步骤与预期中的关键断言
   （数字 / 阈值 / 动作 / 后果）必须能在 PRD 条款（或全文）找到依据——
   数字/阈值断言按（数值, 单位）对校验（边界 ±1 推导值豁免）；
   动作/后果断言按实词命中判定（≥2 个实词命中算有依据）；
   无依据内容标记为「推测项」。判定规则可解释、可单测。
4. 禅道导入前校验器（迭代 2）：对照禅道官方用例导入模板逐字段检查 Excel，
   输出中文校验报告（可导入 / 不可导入 + 错误字段明细）。

全部输出为中文；本模块不做任何网络访问。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from openpyxl import load_workbook

from ..exporters.zentao_excel import ZENTAO_HEADERS
from .schema import CATEGORIES, METHODS, FRItem, TestCase

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


# ---------------------------------------------------------------------------
# 模糊词门禁（迭代 7 / UP-3）：从「警告」升级为「拒绝 + 回灌重试」（阻断接口）
# ---------------------------------------------------------------------------

@dataclass
class FuzzyViolation:
    """模糊词门禁违规明细：用例标题 + 命中词。"""

    title: str
    words: list[str]


def check_fuzzy_gate(cases: Iterable[Union[TestCase, Mapping[str, Any]]]) -> list[FuzzyViolation]:
    """模糊词门禁（阻断接口）：expected 命中 FUZZY_WORDS 即视为校验失败。

    与 check_clarity 同词表同口径；供 LLM 生成链路在结构校验后执行——
    命中即触发回灌重试（见 core/llm.py），mock 模式由模板保证 0 命中。
    """
    violations: list[FuzzyViolation] = []
    for case in cases:
        fuzzy = find_fuzzy_words(_field(case, "expected"))
        if fuzzy:
            violations.append(FuzzyViolation(title=_field(case, "title") or "（无标题）", words=fuzzy))
    return violations


def build_fuzzy_feedback(violations: Sequence[FuzzyViolation]) -> str:
    """构造回灌提示词的中文反馈（含用例标题与命中词）。"""
    parts = [
        f"用例「{v.title}」预期结果含模糊词「{'、'.join(v.words)}」" for v in violations
    ]
    return "；".join(parts) + "。请改写为可验证的确定性表述（具体状态 / 提示语 / 数值）"


def drop_fuzzy_cases(raw_cases: Sequence[Mapping[str, Any]]) -> tuple[list[dict], list[str]]:
    """重试耗尽后的兜底阻断：丢弃仍命中模糊词的用例，返回（保留用例, 中文告警明细）。

    告警逐条列出用例标题与命中词，便于用户定位修正。
    """
    kept: list[dict] = []
    warnings: list[str] = []
    for case in raw_cases:
        data = dict(case)
        fuzzy = find_fuzzy_words(_field(data, "expected"))
        if fuzzy:
            title = _field(data, "title") or "（无标题）"
            warnings.append(
                f"模糊词门禁丢弃用例「{title}」：预期结果含模糊词「{'、'.join(fuzzy)}」，已从用例集中移除"
            )
        else:
            kept.append(data)
    return kept, warnings


# ---------------------------------------------------------------------------
# 幻觉溯源检查辅助（迭代 7 / UP-2）：步骤与预期的关键断言必须有 PRD 依据
# ---------------------------------------------------------------------------

# 数字单位（与 adapters/prd.py 的边界量词保持一致）
_UNIT_PATTERN = r"位|次|分钟|小时|秒|天|件|元|个|岁|字符|张|条|笔|分|%|％|并发|QPS|qps"
# 数值 + 单位断言对：如 30 分钟 / 8 位
_NUM_UNIT_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})")
# 区间：如 8-20 位（两端都是断言值）
_RANGE_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*[-~—～至]\s*(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})")
# 无单位数字（如 OpenAPI "minLength: 8"）：记为（值, ""），作宽松依据（单位不限）
_BARE_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# 行首步骤编号 / 需求编号标签（"1. " / "2、" / "FR-1.1：" / "NFR-1.2 "）不算断言
_STEP_NUM_RE = re.compile(
    r"^\s*(?:(?:N?FR|需求|R)[-\s]?)?\d+(?:\.\d+)*[.、：:．)]\s*", re.IGNORECASE
)
# 断言片段切分字符：句号 / 分号 / 逗号 / 顿号 / 换行 / 感叹疑问句（「」引文内部不切分）
_FRAGMENT_SPLIT_CHARS = "。；;，,、\n!?！？"
# 中文连续串 / 拉丁词（实词抽取素材）
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")

# 推测项两类判定口径
KIND_NUMERIC = "数字/阈值断言无依据"
KIND_ACTION = "动作/后果断言无依据"

# 标准测试推导词（豁免溯源）：边界值推导方向词与通用执行动词——
# 属测试设计的合法推导 / 执行动作，不构成需求规则断言；
# 业务名词（如虚构的「限流 / 降级」规则词、失败 / 成功等业务后果词）
# 不在豁免表内，必须有 PRD 依据。
# 词表仅用于溯源判定的实词抽取，不影响模糊词词表 FUZZY_WORDS。
_DERIVATION_WORDS: tuple[str, ...] = (
    "接受", "拒绝", "触发", "不触发", "未达", "达到", "恰好", "最小值", "最大值",
    "阈值", "边界值", "边界", "相邻值", "越界", "超出", "低于", "高于", "限制",
    "输入", "输出", "提交", "执行", "观察", "准备", "调整", "核对", "记录",
    "测试", "数据", "操作", "处理", "校验", "通过", "请求", "响应", "返回",
    "结果", "系统", "状态", "步骤", "条款", "定义", "一致",
)
_DERIV_SPLIT_RE = re.compile("|".join(map(re.escape, _DERIVATION_WORDS)))

# 虚词 / 语法粘连字：剔除推导词后仅剩这些字的碎片（如「并被」「时被」）
# 属语法连接，不构成规则断言，不参与依据判定
_GLUE_CHARS = frozenset(
    "的了在与和或并且而把被对从由向到于为以之其此每共后前先再还就只"
    "时按给完过很都也该各因所但若如则将请使让得个样点下上中内里外几更最已经"
)


def extract_numeric_tokens(text: str) -> set[tuple[float, str]]:
    """抽取文本中的（数值, 单位）断言对（行首步骤编号不算断言）。

    区间（8-20 位）两端都算断言值；无单位数字记为（值, ""），只作宽松依据。
    """
    tokens: set[tuple[float, str]] = set()
    for raw_line in text.splitlines():
        line = _STEP_NUM_RE.sub("", raw_line)
        occupied: list[tuple[int, int]] = []
        for m in _RANGE_RE.finditer(line):
            tokens.add((float(m.group(1)), m.group(3)))
            tokens.add((float(m.group(2)), m.group(3)))
            occupied.append((m.start(), m.end()))
        for m in _NUM_UNIT_RE.finditer(line):
            if any(s <= m.start() < e for s, e in occupied):
                continue
            tokens.add((float(m.group(1)), m.group(2)))
            occupied.append((m.start(), m.end()))
        for m in _BARE_NUM_RE.finditer(line):
            if any(s <= m.start() < e for s, e in occupied):
                continue
            tokens.add((float(m.group(0)), ""))
    return tokens


def _numeric_grounded(value: float, unit: str, source_pairs: set[tuple[float, str]]) -> bool:
    """数字 / 阈值断言的依据判定：同值同单位 / 同值无单位 / 边界 ±1 推导值均算有依据。"""
    candidates = (
        (value, unit),
        (value, ""),
        (value - 1, unit),
        (value + 1, unit),
        (value - 1, ""),
        (value + 1, ""),
    )
    return any(c in source_pairs for c in candidates)


def _content_tokens(text: str, strip_derivations: bool = True) -> set[str]:
    """抽取实词：中文连续串的二字组合 + 拉丁词（小写）。

    strip_derivations=True 时先剔除标准测试推导词（不构成规则断言），
    仅剩虚词 / 语法粘连字的碎片一并剔除，剩余业务实词才参与依据命中判定。
    """
    tokens: set[str] = set()
    for m in _CJK_RUN_RE.finditer(text):
        run = m.group(0)
        pieces = _DERIV_SPLIT_RE.split(run) if strip_derivations else [run]
        for piece in pieces:
            if len(piece) < 2:
                continue
            if strip_derivations and all(ch in _GLUE_CHARS for ch in piece):
                continue
            tokens.update(piece[i : i + 2] for i in range(len(piece) - 1))
    for m in _LATIN_WORD_RE.finditer(text):
        tokens.add(m.group(0).lower())
    return tokens


def _source_tokens(source_text: str) -> set[str]:
    """PRD 全文的实词命中集合（不剔除推导词：原文任何词都可作依据）。"""
    return _content_tokens(source_text, strip_derivations=False)


def _fmt_value(value: float) -> str:
    """数值格式化（整数不带小数点）。"""
    return f"{value:g}"


def _clause_hint(clause: str) -> str:
    """建议中的条款提示：同一 [FR-x] 条款优先，无对应条款时退回全文兜底说明。"""
    return f"对应条款「{clause}」" if clause else "PRD 全文未见对应条款"


def _split_fragments(text: str) -> list[str]:
    """按句读切分断言片段（确定性）；「」引文是原文引用整体，引文内部不切分。"""
    fragments: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "「":
            depth += 1
        elif ch == "」" and depth > 0:
            depth -= 1
        if depth == 0 and ch in _FRAGMENT_SPLIT_CHARS:
            fragments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    fragments.append("".join(buf))
    return fragments


@dataclass
class GroundingItem:
    """单条「推测项」：用例中无 PRD 依据的断言片段。"""

    index: int  # 用例序号（1 起）
    title: str
    fragment: str  # 无依据片段
    kind: str  # KIND_NUMERIC / KIND_ACTION
    detail: str  # 判定说明
    suggestion: str  # 改进建议（引用用例标题与片段）


@dataclass
class GroundingReport:
    """幻觉溯源校验结果（推测项清单，确定性输出）。"""

    source: str
    total: int
    items: list[GroundingItem] = field(default_factory=list)
    removed: list[GroundingItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """无推测项（且严格模式未删除用例）时为 True。"""
        return not self.items and not self.removed

    @property
    def speculative_indexes(self) -> set[int]:
        """存在无依据断言的用例序号集合（严格模式删除依据）。"""
        return {i.index for i in self.items}

    def format_report(self) -> str:
        """渲染「推测项」专节（全中文 Markdown，点名用例标题 + 无依据片段）。"""
        lines = [
            "## 幻觉溯源校验：推测项",
            "",
            f"- 溯源依据：{self.source or 'PRD 原文'}",
            "- 判定口径：数字 / 阈值断言须在 PRD 条款出现（边界 ±1 推导值豁免）；"
            "动作 / 后果断言按实词命中判定（≥2 个实词命中算有依据）",
            f"- 受检用例：{self.total} 条；推测项：{len(self.items)} 处"
            f"（涉及用例 {len(self.speculative_indexes)} 条）",
        ]
        if self.removed:
            lines.append(f"- 严格模式：已删除推测项用例 {len(self.removed)} 条并告警")
        lines.append("")
        if not self.items and not self.removed:
            lines.append("全部用例的步骤与预期均可在 PRD 找到依据，无推测项。")
            return "\n".join(lines)
        if self.items:
            lines.append("### 无依据断言明细")
            lines.append("")
            for n, item in enumerate(self.items, 1):
                lines.append(f"{n}. 【{item.kind}】{item.suggestion}")
        if self.removed:
            lines.append("")
            lines.append("### 严格模式已删除用例")
            lines.append("")
            for n, item in enumerate(self.removed, 1):
                lines.append(f"{n}. 用例「{item.title}」已删除：{item.detail}")
        return "\n".join(lines)


def check_grounding(
    cases: Iterable[Union[TestCase, Mapping[str, Any]]],
    source_text: str,
    frs: Sequence[FRItem] = (),
    source_name: str = "",
) -> GroundingReport:
    """幻觉溯源校验：用例步骤与预期的关键断言必须能在 PRD 找到依据。

    判定规则（可解释、可单测）：
    - 数字 / 阈值断言：（数值, 单位）对须在 PRD 条款原文出现（同一 [FR-x] 条款
      优先，全文兜底）；边界 ±1 类测试推导值豁免溯源（属合法推导）。
    - 动作 / 后果断言：按关键词覆盖判定——片段实词（中文二字组合 + 拉丁词，
      剔除标准测试推导词）≥2 个命中 PRD 原文算有依据。
    - 无依据内容标记为「推测项」，报告专列一节点名用例标题 + 无依据片段 + 建议；
      --strict 由调用方（evals/judge.py）按 speculative_indexes 删除用例。
    """
    items: list[GroundingItem] = []
    source_tokens = _source_tokens(source_text)
    source_pairs = extract_numeric_tokens(source_text)
    rows = list(cases)
    for idx, case in enumerate(rows, 1):
        title = _field(case, "title") or "（无标题）"
        clause = ""
        for fr in frs:
            if fr.tag in title:
                clause = fr.text
                break
        text = f"{_field(case, 'steps')}\n{_field(case, 'expected')}"
        seen: set[str] = set()
        for fragment in _split_fragments(text):
            frag = fragment.strip()
            if not frag or frag in seen:
                continue
            seen.add(frag)
            # 1) 数字 / 阈值断言：数值 + 单位对须有 PRD 依据（±1 推导豁免）
            bad_nums = [
                (value, unit)
                for value, unit in extract_numeric_tokens(frag)
                if unit and not _numeric_grounded(value, unit, source_pairs)
            ]
            if bad_nums:
                nums = "、".join(f"{_fmt_value(v)}{u}" for v, u in bad_nums)
                items.append(
                    GroundingItem(
                        index=idx,
                        title=title,
                        fragment=frag,
                        kind=KIND_NUMERIC,
                        detail=f"数值 {nums} 未在 PRD 条款出现（且非边界 ±1 推导值）",
                        suggestion=(
                            f"用例「{title}」片段「{frag}」中的 {nums} 无 PRD 依据"
                            f"（疑似规则改写：{_clause_hint(clause)}）；"
                            "请改回条款原文数值，或标记为推测项并删除"
                        ),
                    )
                )
                continue
            # 2) 动作 / 后果断言：实词命中 ≥2 个算有依据
            tokens = _content_tokens(frag)
            if not tokens:
                continue
            hits = tokens & source_tokens
            if len(hits) >= 2 or (len(tokens) == 1 and len(hits) >= 1):
                continue
            items.append(
                GroundingItem(
                    index=idx,
                    title=title,
                    fragment=frag,
                    kind=KIND_ACTION,
                    detail=f"实词命中 {len(hits)} 个（{'、'.join(sorted(hits)) or '无'}），低于 2 个依据下限",
                    suggestion=(
                        f"用例「{title}」片段「{frag}」疑似虚构 / 改写规则"
                        f"（{_clause_hint(clause)}）；请引用 PRD 条款原文改写，或删除该断言"
                    ),
                )
            )
    return GroundingReport(source=source_name, total=len(rows), items=items)


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
