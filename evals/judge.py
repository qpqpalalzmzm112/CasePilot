"""LLM-as-a-Judge 用例质量自评（核心差异化第 3 项，迭代 3 / UP-2）。

输入：禅道格式用例 Excel（与 gen 产出兼容，Sheet「用例」+ 固定 8 列）。
输出：全中文评分报告（4 维度得分 + 总分 + 改进建议，建议引用具体用例标题）。

评审维度（每维 1-5 分，5 为最佳；总分 = 4 维平均，口径 1-5）：
1. 完整性：8 要素非空且具体
2. 方法论标注合理性：关键词列标注合法且与用例内容相符
3. 优先级合理性：优先级分布有区分度（主流程 P0 / 异常边界 P2）
4. 可执行性：步骤可操作、预期结果有可校验的判定标准

双模式（沿用 LLM 网关的模式决策）：
- LLM 评审：有 API key（或本地 Ollama 可达）时逐条评审打分，
  temperature=0、要求 JSON 输出，解析 / 校验失败自动重试 ≤2 次（错误信息回灌提示词）
- 规则评分降级：无 key 且 Ollama 不可达时，按可离线判定、可解释的规则打 1-5 分
  （如「预期结果为空→完整性 1 分」），并给出中文改进建议

结果确定性：mock / 规则模式下同输入同报告（不含时间戳）；密钥只走环境变量。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from openpyxl import load_workbook

from casepilot.core import llm as llm_gateway
from casepilot.core.quality import FUZZY_WORDS, find_fuzzy_words
from casepilot.core.schema import CATEGORIES, METHODS, PRIORITIES
from casepilot.exporters.zentao_excel import ZENTAO_HEADERS

logger = logging.getLogger("casepilot")

# 评审维度固定名称与顺序
DIM_COMPLETENESS = "完整性"
DIM_METHODOLOGY = "方法论标注合理性"
DIM_PRIORITY = "优先级合理性"
DIM_EXECUTABILITY = "可执行性"
JUDGE_DIMENSIONS: tuple[str, ...] = (
    DIM_COMPLETENESS,
    DIM_METHODOLOGY,
    DIM_PRIORITY,
    DIM_EXECUTABILITY,
)

# 禅道数字优先级 → P0-P3 反向映射
_PRIORITY_REVERSE = {"1": "P0", "2": "P1", "3": "P2", "4": "P3"}

# 用例 8 要素字段（与 TestCase / 禅道列对应）
CASE_FIELDS: tuple[str, ...] = (
    "title", "precondition", "steps", "expected", "priority", "category", "method", "module",
)

# 步骤编号识别（"1. " / "2、" 等）
_STEP_RE = re.compile(r"^\s*\d+[.、)]", re.MULTILINE)

# 预期结果「具体」判定：出现数字、引号提示语、或足够长度的判定描述
_CONCRETE_RE = re.compile(r"\d|「|」")

# 通用套话（判定标准不具体的表现），命中即「具体」降档
_GENERIC_PHRASES = (
    "系统处于功能可用状态",
    "返回结果符合预期",
    "系统行为符合需求规则定义",
    "符合预期，功能正常",
)

# 建议下限：报告至少给出 3 条引用具体用例标题的改进建议
MIN_SUGGESTIONS = 3

# 字段英文键 → 中文名（报告与建议统一中文）
_FIELD_LABELS = {
    "title": "用例标题", "precondition": "前置条件", "steps": "步骤", "expected": "预期结果",
    "priority": "优先级", "category": "用例类型", "method": "方法论", "module": "所属模块",
}


def _labels(fields) -> str:
    """字段键列表转中文名列表。"""
    return "、".join(_FIELD_LABELS.get(f, f) for f in fields)


def _cell(value: Any) -> str:
    """单元格取值归一为去空白字符串（空单元格 → 空串）。"""
    return "" if value is None else str(value).strip()


def _normalize_priority(value: Any) -> str:
    """优先级归一：禅道数字 1-4 → P0-P3，P0-P3 原样保留，其余保留原文供规则判定。"""
    text = _cell(value)
    return _PRIORITY_REVERSE.get(text, text)


def load_cases_from_excel(path: Union[str, Path]) -> list[dict]:
    """读取禅道格式用例 Excel 为原始用例 dict 列表（保留空值，供质量评审）。

    列映射按 ZENTAO_HEADERS 固定顺序：所属模块 / 用例标题 / 前置条件 / 步骤 /
    预期结果 / 优先级 / 用例类型 / 关键词。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"用例文件不存在：{p}")
    try:
        wb = load_workbook(p)
    except Exception as exc:  # noqa: BLE001 - 无法打开统一转中文错误
        raise ValueError(f"无法打开 Excel 文件（格式非法或已损坏）：{p}（{exc}）") from exc
    if "用例" not in wb.sheetnames:
        raise ValueError(
            f"缺少「用例」工作表（禅道导入模板 Sheet 名为「用例」，实际：{'、'.join(wb.sheetnames) or '无'}）"
        )
    ws = wb["用例"]
    cases: list[dict] = []
    for row in range(2, ws.max_row + 1):
        values = [ws.cell(row=row, column=col).value for col in range(1, len(ZENTAO_HEADERS) + 1)]
        if not any(_cell(v) for v in values):
            continue  # 整行空白跳过
        case = {
            "title": _cell(values[1]),
            "precondition": _cell(values[2]),
            "steps": _cell(values[3]),
            "expected": _cell(values[4]),
            "priority": _normalize_priority(values[5]),
            "category": _cell(values[6]),
            "method": _cell(values[7]),
            "module": _cell(values[0]),
        }
        cases.append(case)
    if not cases:
        raise ValueError(f"用例文件没有任何用例数据行：{p}")
    return cases


@dataclass
class JudgeReport:
    """用例质量评审结果（双模式通用，确定性输出）。"""

    mode: str  # "llm" = LLM 评审，"rule" = 规则评分降级
    mode_note: str
    case_count: int
    scores: dict[str, float]  # 维度 → 1-5 分
    comments: dict[str, str]  # 维度 → 一句话解读
    suggestions: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        """总分（口径 1-5）：4 个维度平均，保留 1 位小数。"""
        return round(sum(self.scores[d] for d in JUDGE_DIMENSIONS) / len(JUDGE_DIMENSIONS), 1)

    def format_report(self, source: str) -> str:
        """渲染全中文 Markdown 评审报告（不含时间戳，同输入同报告）。"""
        lines = [
            "# 用例质量评审报告（LLM-as-a-Judge）",
            "",
            f"- 评审对象：{source}（{self.case_count} 条用例）",
            f"- 评审模式：{self.mode_note}",
            "- 评分口径：各维度 1-5 分（5 为最佳），总分 = 4 个维度平均分（1-5），百分制 = 总分 × 20",
            "",
            f"## 总分：{self.total} / 5（百分制 {self.total * 20:.0f}）",
            "",
            "## 维度得分",
            "",
            "| 维度 | 得分 | 一句话解读 |",
            "| --- | --- | --- |",
        ]
        for dim in JUDGE_DIMENSIONS:
            lines.append(f"| {dim} | {self.scores[dim]} | {self.comments[dim]} |")
        lines.append("")
        lines.append("## 改进建议")
        lines.append("")
        for i, s in enumerate(self.suggestions, 1):
            lines.append(f"{i}. {s}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 规则评分降级（无 key 时的可解释离线评分）
# ---------------------------------------------------------------------------

def _step_count(steps: str) -> int:
    """统计编号步骤数。"""
    return len(_STEP_RE.findall(steps))


def _score_completeness(case: dict) -> tuple[int, Optional[str]]:
    """完整性：8 要素非空且具体。

    规则（可解释）：预期结果为空 → 1 分；其他要素每缺一项 -2（下限 1）；
    全部非空但存在不足 6 字的要素或通用套话（如「系统处于功能可用状态」）→ 4 分
    （具体性不足）；否则 5 分。
    """
    missing = [f for f in CASE_FIELDS if not case.get(f, "").strip()]
    if "expected" in missing:
        return 1, "预期结果为空"
    if missing:
        return max(1, 5 - 2 * len(missing)), f"{_labels(missing)}为空"
    short = [
        f for f in ("title", "precondition", "steps", "expected")
        if len(case.get(f, "").strip()) < 6
    ]
    if short:
        return 4, f"{_labels(short)}内容不足 6 字，要素过于笼统"
    generic = [
        f for f in ("title", "precondition", "steps", "expected")
        if any(p in case.get(f, "") for p in _GENERIC_PHRASES)
    ]
    if generic:
        return 4, f"{_labels(generic)}含通用套话（如「系统处于功能可用状态」），判定标准不够具体"
    return 5, None


def _score_methodology(case: dict) -> tuple[int, Optional[str]]:
    """方法论标注合理性：标注合法且与内容相符。"""
    method = case.get("method", "").strip()
    if not method:
        return 1, "方法论标注为空"
    if method not in METHODS:
        return 1, f"方法论标注「{method}」不在枚举（{'/'.join(METHODS)}）内"
    body = f"{case.get('steps', '')}{case.get('expected', '')}"
    if method == "边界值" and not re.search(r"\d", body):
        return 3, "标注为边界值但步骤与预期结果均无具体数值"
    if method == "场景法" and _step_count(case.get("steps", "")) < 2:
        return 3, "标注为场景法但操作步骤不足 2 步"
    return 5, None


def _score_executability(case: dict) -> tuple[int, Optional[str]]:
    """可执行性：步骤可操作、预期结果有可校验的判定标准。

    迭代 6 / UP-2：预期结果含模糊词（FUZZY_WORDS，复用 core/quality.py 词表）
    直接扣到 2 分——「或 / 可能 / 视情况」类表述无法形成唯一的通过 / 失败判定。
    """
    steps = case.get("steps", "").strip()
    expected = case.get("expected", "").strip()
    if not steps:
        return 1, "操作步骤为空，用例无法执行"
    if not expected:
        return 1, "预期结果为空，执行后无法判定通过"
    fuzzy = find_fuzzy_words(expected)
    if fuzzy:
        return 2, (
            f"预期结果含模糊词「{'、'.join(fuzzy)}」，"
            "执行后无法形成唯一通过 / 失败判定，请改为明确判定标准"
        )
    numbered = _step_count(steps) >= 2
    concrete = bool(_CONCRETE_RE.search(expected)) or len(expected) >= 15
    if numbered and concrete:
        return 5, None
    if numbered:
        return 3, "预期结果缺少可校验的判定标准（无具体提示语 / 状态 / 数值）"
    return 2, "操作步骤不足 2 步，执行路径不清晰"


def _score_priority(cases: Sequence[dict]) -> tuple[int, Optional[str]]:
    """优先级合理性（用例集层面）：分布有区分度。"""
    if not cases:
        return 1, "用例集为空"
    priorities = [c.get("priority", "").strip() for c in cases]
    invalid = [p for p in priorities if p not in PRIORITIES]
    if invalid:
        return 2, f"存在非法优先级取值（{'、'.join(sorted(set(invalid))[:3])}）"
    distinct = set(priorities)
    p0_ratio = priorities.count("P0") / len(priorities)
    if distinct == {"P0"}:
        return 1, "优先级全部为 P0，缺少分级，失去回归筛选价值"
    if p0_ratio > 0.6:
        return 2, f"P0 占比 {p0_ratio:.0%} 过高，优先级失去区分度"
    if len(distinct) >= 3 and p0_ratio <= 0.5:
        return 5, None
    return 3, "优先级分布区分度一般（建议覆盖 P0/P1/P2 三档）"


_ADVICE_BY_DIMENSION = {
    DIM_COMPLETENESS: "建议补齐空缺要素，并写明可校验的具体内容",
    DIM_METHODOLOGY: "建议按用例实际设计方法修正「关键词」列标注，使标注与设计内容一致",
    DIM_PRIORITY: "建议按「主流程 P0 / 有效类与边界 P1 / 异常越界 P2」重新分布优先级",
    DIM_EXECUTABILITY: "建议把步骤拆为编号步骤，并在预期结果写明判定标准（具体提示语 / 状态 / 数值）",
}


def rule_review(cases: Sequence[dict]) -> JudgeReport:
    """规则评分降级：每维度按可离线判定的规则打 1-5 分（确定性）。"""
    per_case: dict[str, list[tuple[int, Optional[str]]]] = {
        DIM_COMPLETENESS: [], DIM_METHODOLOGY: [], DIM_EXECUTABILITY: [],
    }
    findings: list[tuple[str, str, str]] = []  # (标题, 维度, 问题)
    case_totals: list[tuple[float, str]] = []
    for case in cases:
        title = case.get("title", "").strip() or "（无标题）"
        scores = {}
        for dim, scorer in (
            (DIM_COMPLETENESS, _score_completeness),
            (DIM_METHODOLOGY, _score_methodology),
            (DIM_EXECUTABILITY, _score_executability),
        ):
            score, issue = scorer(case)
            per_case[dim].append((score, issue))
            scores[dim] = score
            if issue:
                findings.append((title, dim, issue))
        case_totals.append(
            (sum(scores.values()) / len(scores), title)
        )

    priority_score, priority_issue = _score_priority(cases)

    def _mean(dim: str) -> float:
        return round(sum(s for s, _ in per_case[dim]) / len(per_case[dim]), 1)

    scores = {
        DIM_COMPLETENESS: _mean(DIM_COMPLETENESS),
        DIM_METHODOLOGY: _mean(DIM_METHODOLOGY),
        DIM_PRIORITY: float(priority_score),
        DIM_EXECUTABILITY: _mean(DIM_EXECUTABILITY),
    }
    comments = {
        DIM_COMPLETENESS: (
            f"{sum(1 for s, _ in per_case[DIM_COMPLETENESS] if s == 5)}/{len(cases)} 条用例 8 要素非空且具体"
        ),
        DIM_METHODOLOGY: (
            f"{sum(1 for s, _ in per_case[DIM_METHODOLOGY] if s == 5)}/{len(cases)} 条用例方法论标注合法且与内容相符"
        ),
        DIM_PRIORITY: priority_issue or f"{len(set(c.get('priority', '') for c in cases))} 档优先级分布合理，P0 保留给核心路径",
        DIM_EXECUTABILITY: (
            f"{sum(1 for s, _ in per_case[DIM_EXECUTABILITY] if s == 5)}/{len(cases)} 条用例步骤可执行且预期含判定标准"
        ),
    }

    # 改进建议：逐条发现按维度取代表性 3 条（引用具体用例标题），
    # 优先级问题始终保留，最后按最弱用例补齐到 ≥3 条
    suggestions: list[str] = []
    per_dim_count: dict[str, int] = {}
    for title, dim, issue in findings:
        if per_dim_count.get(dim, 0) >= 3:
            continue
        per_dim_count[dim] = per_dim_count.get(dim, 0) + 1
        suggestions.append(
            f"用例「{title}」的{dim}被判低分：{issue}；{_ADVICE_BY_DIMENSION[dim]}"
        )
    if priority_issue:
        suggestions.append(f"用例集优先级问题：{priority_issue}；{_ADVICE_BY_DIMENSION[DIM_PRIORITY]}")
    if len(suggestions) < MIN_SUGGESTIONS:
        weakest = sorted(case_totals, key=lambda t: t[0])
        for _, title in weakest:
            if len(suggestions) >= MIN_SUGGESTIONS:
                break
            suggestions.append(
                f"用例「{title}」建议核对预期结果是否可自动校验（具体提示语 / 状态 / 数值），"
                f"并在禅道中补充前置测试数据说明，便于回归复用"
            )
    return JudgeReport(
        mode="rule",
        mode_note="规则降级（无 API key 且本地 Ollama 不可达，采用可解释离线规则评分）",
        case_count=len(cases),
        scores=scores,
        comments=comments,
        suggestions=suggestions[:10],  # 建议精而不多：每维代表性 3 条 + 优先级问题，下限 MIN_SUGGESTIONS 由上方补齐保证
    )


# ---------------------------------------------------------------------------
# LLM 评审（有 key / Ollama 可用时）
# ---------------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT = (
    "你是资深测试用例评审专家，请逐条评审测试用例并按 4 个维度打分（每维 1-5 整数分，5 为最佳）：\n"
    f"1. {DIM_COMPLETENESS}：8 要素（标题/前置条件/步骤/预期结果/优先级/类型/方法论/模块）非空且具体\n"
    f"2. {DIM_METHODOLOGY}：方法论标注合法（等价类/边界值/场景法/判定表/安全测试/性能测试）且与用例设计内容相符\n"
    f"3. {DIM_PRIORITY}：优先级（P0-P3）取值合理、用例集分布有区分度\n"
    f"4. {DIM_EXECUTABILITY}：步骤可操作、预期结果有可校验的判定标准，"
    f"预期结果不得含模糊词（{'/'.join(FUZZY_WORDS)}）\n"
    "输出要求：只输出一个合法 JSON 对象，不要任何解释文字或 markdown 代码块，结构为：\n"
    '{"cases": [{"title": "用例标题", "scores": {"'
    + DIM_COMPLETENESS + '": 5, "' + DIM_METHODOLOGY + '": 5, "'
    + DIM_PRIORITY + '": 5, "' + DIM_EXECUTABILITY + '": 5}, "comment": "一句话评审意见"}],\n'
    ' "suggestions": ["引用具体用例标题的中文改进建议（至少 3 条）"]}'
)


def build_judge_prompt(cases: Sequence[dict]) -> str:
    """构建 LLM 评审 user prompt：逐条列出用例 8 要素。"""
    lines = ["请评审以下测试用例并输出 JSON："]
    for i, case in enumerate(cases, 1):
        lines.append(f"\n【用例 {i}】")
        labels = {
            "title": "用例标题", "precondition": "前置条件", "steps": "步骤",
            "expected": "预期结果", "priority": "优先级", "category": "用例类型",
            "method": "方法论", "module": "所属模块",
        }
        for key in CASE_FIELDS:
            lines.append(f"{labels[key]}：{case.get(key, '')}")
    return "\n".join(lines)


def parse_judge_json(content: str) -> dict:
    """从 LLM 输出解析评审 JSON（容忍 ```json 代码块），失败抛清晰中文错误。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 评审输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list) or not data["cases"]:
        raise ValueError("LLM 评审输出应为含非空 cases 数组的 JSON 对象")
    for i, item in enumerate(data["cases"], 1):
        if not isinstance(item, dict) or not isinstance(item.get("scores"), dict):
            raise ValueError(f"LLM 评审输出第 {i} 条缺少 scores 对象")
        for dim in JUDGE_DIMENSIONS:
            score = item["scores"].get(dim)
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 1 <= score <= 5:
                raise ValueError(f"LLM 评审输出第 {i} 条「{dim}」分数应为 1-5 的数值，实际为 {score!r}")
    return data


def llm_review(cases: Sequence[dict], model_note: Optional[str] = None) -> JudgeReport:
    """LLM 逐条评审打分（temperature=0、JSON 输出，解析失败自动重试 ≤2 次）。"""
    system = JUDGE_SYSTEM_PROMPT
    user = build_judge_prompt(cases)
    base_url = llm_gateway.get_base_url("openai" if llm_gateway.get_api_key() else "ollama")
    api_key = llm_gateway.get_api_key()
    model = llm_gateway.get_model("openai" if api_key else "ollama")

    max_retries = llm_gateway.get_max_retries()
    last_error: Optional[ValueError] = None
    data: Optional[dict] = None
    for attempt in range(max_retries + 1):
        prompt = user
        if last_error is not None:
            prompt = (
                f"{user}\n\n"
                f"【自动重试 第 {attempt} 次】你上一次的输出存在问题：{last_error}\n"
                "请修正后重新输出：只输出一个合法 JSON 对象，不要任何解释文字。"
            )
        content = llm_gateway.call_chat_completions(base_url, api_key, model, system, prompt)
        try:
            data = parse_judge_json(content)
            break
        except ValueError as exc:
            last_error = exc
            if attempt < max_retries:
                logger.warning("LLM 评审输出解析失败（第 %d 次自动重试）：%s", attempt + 1, exc)
    if data is None:
        raise ValueError(
            f"LLM 评审输出解析连续失败，已自动重试 {max_retries} 次仍不成功；最后一次错误：{last_error}"
        )

    items = data["cases"]
    scores = {
        dim: round(sum(float(it["scores"][dim]) for it in items) / len(items), 1)
        for dim in JUDGE_DIMENSIONS
    }
    comments = {
        dim: f"LLM 逐条评审 {len(items)} 条用例的平均分（{dim}）" for dim in JUDGE_DIMENSIONS
    }
    suggestions = [str(s) for s in data.get("suggestions", []) if str(s).strip()]
    if len(suggestions) < MIN_SUGGESTIONS:
        # 模型建议不足时用规则建议补齐，保证报告至少 3 条可执行改进项
        fallback = rule_review(cases).suggestions
        for s in fallback:
            if len(suggestions) >= MIN_SUGGESTIONS:
                break
            if s not in suggestions:
                suggestions.append(s)
    return JudgeReport(
        mode="llm",
        mode_note=f"LLM 评审（模型 {model_note or model}，temperature=0，逐条打分后聚合）",
        case_count=len(cases),
        scores=scores,
        comments=comments,
        suggestions=suggestions,
    )


def resolve_judge_mode() -> str:
    """评审模式决策（沿用 LLM 网关）：mock → 规则降级，其余 → LLM 评审。"""
    return "rule" if llm_gateway.resolve_mode() == "mock" else "llm"


def judge_cases(cases: Sequence[dict], mode: str = "auto") -> JudgeReport:
    """用例质量自评入口：mode=auto 时按 LLM 网关模式自动选择双模式之一。"""
    if not cases:
        raise ValueError("用例集为空，无法评审")
    resolved = resolve_judge_mode() if mode == "auto" else mode
    if resolved == "llm":
        return llm_review(cases)
    return rule_review(cases)
