"""本地质检器（迭代 10 / UP-1）：`casepilot doctor` 的纯本地规则质检（全中文输出）。

复用 core/quality.py 全部检查器（FUZZY_WORDS / 弱断言 / 同义反复死短语 / 测量值预言 /
契约发明——词表唯一事实来源，本模块禁止复制词表），并新增两项对账类检查：
- 编号引用一致性：用例标题 [FR-x] 追溯标签与 --source 原文编号逐字比对，
  引用不存在的编号（断链编号）与未被引用的原文编号分别登记；
- 边界成对性：边界值用例（method=边界值）预期须同时出现边界值两侧相邻值
  （v-1 / v+1，同单位或无单位形态，时间复合 / 时点形态同口径）。

严重度分节（阻断项 / 警告 / 提示），逐条点名用例标题 + 命中内容 + 修改建议；
默认零 LLM 调用、零 token；--llm-review 只把本地判定为可疑的条目送 LLM 复核
（有 API key 才生效，无 key 跳过并中文提示），复核结论合并进报告。
退出码口径：有阻断项 exit 1，仅警告 / 全过 exit 0（由 CLI 执行）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from openpyxl import load_workbook

from ..exporters.markdown_export import ELEMENT_ROWS
from ..exporters.zentao_excel import ZENTAO_HEADERS, split_keyword
from . import llm as llm_gateway
from . import quality
from .schema import FRItem

# 严重度三档（报告分节与退出码口径的唯一来源）
SEVERITY_BLOCK = "阻断项"
SEVERITY_WARN = "警告"
SEVERITY_INFO = "提示"
SEVERITIES = (SEVERITY_BLOCK, SEVERITY_WARN, SEVERITY_INFO)

# 检查项中文名（报告点名用）
CHECK_FUZZY = "模糊词"
CHECK_WEAK = "弱断言"
CHECK_TAUTOLOGY = "同义反复死短语"
CHECK_MEASUREMENT = "测量值预言"
CHECK_CONTRACT = "契约发明"
CHECK_NUMBERING = "编号引用一致性"
CHECK_BOUNDARY = "边界成对性"
CHECK_ASSUMPTION = "设计假设标注"

# doctor 检查项全集（报告头部说明）
CHECK_LIST_TEXT = (
    "模糊词（含引文豁免）/ 弱断言 / 同义反复死短语 / 测量值预言 / "
    "契约发明 / 编号引用一致性 / 边界成对性"
)

# 用例标题中的追溯标签：[FR-1.1] / [NFR-1.2] / [FR-auto-1]（逐字比对原文编号）
_TAG_RE = re.compile(r"\[([^\[\]]+)\]")
_ID_LIKE_RE = re.compile(r"^(?:NFR|FR|需求|R)", re.IGNORECASE)
# Markdown 用例表「测试方法论」单元格的假设后缀（导出侧为「（设计假设）」）
ASSUMPTION_SUFFIX = "假设"


@dataclass
class DoctorFinding:
    """单条质检发现：严重度 + 检查项 + 用例标题 + 命中内容 + 修改建议。"""

    severity: str
    check: str
    title: str
    hit: str
    suggestion: str


@dataclass
class DoctorReport:
    """本地质检结果（中文报告，确定性输出、不含时间戳）。"""

    source_file: str
    case_count: int
    source_name: str = ""
    findings: list[DoctorFinding] = field(default_factory=list)
    llm_conclusions: list[str] = field(default_factory=list)

    @property
    def blockers(self) -> list[DoctorFinding]:
        """阻断项（有阻断 ⇒ CLI exit 1）。"""
        return [f for f in self.findings if f.severity == SEVERITY_BLOCK]

    @property
    def warnings(self) -> list[DoctorFinding]:
        """警告项（不阻断退出码）。"""
        return [f for f in self.findings if f.severity == SEVERITY_WARN]

    @property
    def infos(self) -> list[DoctorFinding]:
        """提示项（说明 / 提醒类）。"""
        return [f for f in self.findings if f.severity == SEVERITY_INFO]

    @property
    def has_blockers(self) -> bool:
        """存在阻断项时为 True（CLI 据此 exit 1）。"""
        return bool(self.blockers)

    def format_report(self) -> str:
        """渲染全中文质检报告：按严重度分节，逐条点名标题 + 命中 + 建议。"""
        lines = [
            f"本地质检报告（casepilot doctor）：{self.source_file}",
            f"- 受检用例：{self.case_count} 条",
            f"- 检查项：{CHECK_LIST_TEXT}",
            f"- 溯源依据：{self.source_name or '未提供 --source（溯源类比对按提示降级）'}",
            "",
        ]
        for severity in SEVERITIES:
            items = [f for f in self.findings if f.severity == severity]
            lines.append(f"## {severity}（{len(items)}）")
            lines.append("")
            if not items:
                lines.append("无")
                lines.append("")
                continue
            for n, item in enumerate(items, 1):
                lines.append(
                    f"{n}. 用例「{item.title}」〔{item.check}〕命中「{item.hit}」；建议：{item.suggestion}"
                )
            lines.append("")
        if self.llm_conclusions:
            lines.append("## LLM 复核结论（--llm-review）")
            lines.append("")
            for n, text in enumerate(self.llm_conclusions, 1):
                lines.append(f"{n}. {text}")
            lines.append("")
        if self.has_blockers:
            lines.append(
                f"质检结论：不通过——存在阻断项 {len(self.blockers)} 条"
                f"（警告 {len(self.warnings)} 条、提示 {len(self.infos)} 条），请按明细修正后重新质检"
            )
        elif self.warnings:
            lines.append(
                f"质检结论：通过（无阻断项；警告 {len(self.warnings)} 条、提示 {len(self.infos)} 条不阻断）"
            )
        else:
            lines.append(f"质检结论：通过（全部检查项零阻断零警告，提示 {len(self.infos)} 条）")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 用例文件读取：禅道 Excel（*_cases.xlsx）与 Markdown 用例表（*_cases.md）
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    """单元格取值归一为去空白字符串（空单元格 → 空串）。"""
    return "" if value is None else str(value).strip()


def load_cases_from_xlsx(path: Union[str, Path]) -> list[dict]:
    """读取禅道格式用例 Excel（Sheet「用例」+ 固定 8 列）为用例 dict 列表。

    关键词列反解析出方法论与「,假设」标注（保留 assumption 供提示项检查）。
    """
    p = Path(path)
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
        method, assumed = split_keyword(_cell(values[7]))
        cases.append(
            {
                "title": _cell(values[1]),
                "precondition": _cell(values[2]),
                "steps": _cell(values[3]),
                "expected": _cell(values[4]),
                "priority": _cell(values[5]),
                "category": _cell(values[6]),
                "method": method,
                "module": _cell(values[0]),
                "assumption": assumed,
            }
        )
    return cases


_MD_ROW_RE = re.compile(r"^\|(.*)\|\s*$")


def _md_cells(line: str) -> Optional[list[str]]:
    """解析 Markdown 表格行（内容竖线按 \\| 转义），非表格行返回 None。"""
    match = _MD_ROW_RE.match(line.strip())
    if not match:
        return None
    parts = re.split(r"(?<!\\)\|", match.group(1))
    return [p.strip().replace("\\|", "|") for p in parts]


def _unescape_md(value: str) -> str:
    """还原 Markdown 导出的单元格转义（换行 <br>、竖线 \\|）。"""
    return value.replace("<br>", "\n").replace("\\|", "|")


def load_cases_from_markdown(path: Union[str, Path]) -> list[dict]:
    """读取 Markdown 用例表（--emit md 导出格式）为用例 dict 列表。

    每条用例为 `### <用例标题>` + 8 要素表格；尾部「方法论统计」表自动忽略。
    """
    text = Path(path).read_text(encoding="utf-8")
    cases: list[dict] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        if line.startswith("### "):
            current = {
                "title": line[4:].strip(),
                "precondition": "",
                "steps": "",
                "expected": "",
                "priority": "",
                "category": "",
                "method": "",
                "module": "",
                "assumption": False,
            }
            cases.append(current)
            continue
        cells = _md_cells(line)
        if not current or not cells or len(cells) < 2 or cells[0] not in ELEMENT_ROWS:
            continue
        name, value = cells[0], _unescape_md(cells[1])
        if name == "用例标题":
            current["title"] = value
        elif name == "前置条件":
            current["precondition"] = value
        elif name == "步骤":
            current["steps"] = value
        elif name == "预期结果":
            current["expected"] = value
        elif name == "优先级":
            current["priority"] = value
        elif name == "用例类型":
            current["category"] = value
        elif name == "测试方法论":
            method, assumed = split_keyword(value.replace("（设计假设）", f",{ASSUMPTION_SUFFIX}"))
            current["method"] = method
            current["assumption"] = assumed
        elif name == "所属模块":
            current["module"] = value
    return cases


def load_cases(path: Union[str, Path]) -> list[dict]:
    """按扩展名读取用例文件（.xlsx 禅道 Excel / .md Markdown 用例表）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"用例文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix == ".xlsx":
        cases = load_cases_from_xlsx(p)
    elif suffix == ".md":
        cases = load_cases_from_markdown(p)
    else:
        raise ValueError(f"暂不支持的用例文件格式：{suffix or '（无扩展名）'}，当前支持 .xlsx（禅道 Excel）与 .md（Markdown 用例表）")
    if not cases:
        raise ValueError(f"用例文件没有任何用例数据行：{p}（无可质检内容）")
    return cases


# ---------------------------------------------------------------------------
# 边界成对性（迭代 10 / UP-1）：边界用例预期须有两侧相邻值 v-1 / v+1
# ---------------------------------------------------------------------------

# 科学计数法数值（如 1e+06）归一为普通数字；只匹配含 e/E 的形态，
# 不动普通数字与「07:00」时点的前导零
_EXPONENT_RE = re.compile(r"\d+(?:\.\d+)?[eE][+-]?\d+")
# 时点形态（06:59 / 07:00）：行首「07:」会被步骤编号正则误吞，抽取前先整体占位
_TIMEPOINT_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
_TIMEPOINT_PLACEHOLDER = "时点 "


def _plain_num(value: float) -> str:
    """科学计数法归一为普通数字文本（1e+06 → 1000000）。"""
    return f"{value:f}".rstrip("0").rstrip(".") or "0"


def _display_value(value: float, unit: str) -> str:
    """（数值, 单位）断言对的展示文本：时点按 HH:MM，其余按「数值+单位」。"""
    if unit == "时点":
        total = int(round(value)) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"
    return f"{_plain_num(value)}{unit}"


def numeric_assertion_tokens(text: str) -> set[tuple[float, str]]:
    """预期结果的（数值, 单位）断言对（时间复合 / 时点 / 区间与溯源判定同口径）。

    - 边界 ±1 推导注记（「（少 1）」「（多 1）」「（切换时刻前 1 分钟）」）整体剥离，
      注记内数字不算断言值（与溯源判定 _DERIVATION_CONTEXT_RE 同口径）；
    - 时点（06:59 / 07:00）先整体抽为（分钟数, 「时点」）（行首冒号形态会被
      步骤编号正则误吞，先占位保护），剩余文本复用 quality 抽取。
    """
    rest = quality._DERIVATION_CONTEXT_RE.sub("", text)
    tokens: set[tuple[float, str]] = set()

    def _take_timepoint(match: re.Match) -> str:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23 and minute <= 59:
            tokens.add((float(hour * 60 + minute), "时点"))
        return _TIMEPOINT_PLACEHOLDER

    rest = _TIMEPOINT_RE.sub(_take_timepoint, rest)
    rest = _EXPONENT_RE.sub(lambda m: _plain_num(float(m.group(0))), rest)
    return tokens | quality.extract_numeric_tokens(rest)


def find_missing_adjacent(expected: str) -> Optional[str]:
    """边界成对性判定：预期须同时出现某边界值的两侧相邻值（v-1 / v+1）。

    成对完整返回 None；否则返回中文缺失说明（点名边界值与缺失的相邻值）。
    相邻值接受同单位 / 无单位形态（「1800秒」「29 分 59 秒」「1799 秒」互通，
    时间点 06:59 / 07:00 / 07:01 按分钟相邻判定）。
    """
    tokens = numeric_assertion_tokens(expected)
    if not tokens:
        return "预期结果未出现任何数值（边界值用例缺少边界值与两侧相邻值）"
    by_unit: dict[str, set[float]] = {}
    for value, unit in tokens:
        by_unit.setdefault(unit, set()).add(value)

    def has_side(value: float, unit: str, delta: int) -> bool:
        return (value + delta) in by_unit.get(unit, set()) or (value + delta) in by_unit.get("", set())

    best: Optional[tuple[int, float, str]] = None
    for value, unit in sorted(tokens):
        score = int(has_side(value, unit, -1)) + int(has_side(value, unit, 1))
        # 平手时取较大值点名（「5 与 4（少 1）」场景点名主边界值 5 而非相邻值 4）
        if best is None or score > best[0] or (score == best[0] and value > best[1]):
            best = (score, value, unit)
        if score == 2:
            return None
    _score, value, unit = best  # type: ignore[misc]
    missing = []
    if not has_side(value, unit, -1):
        missing.append(f"{_display_value(value - 1, unit)}（少 1）")
    if not has_side(value, unit, 1):
        missing.append(f"{_display_value(value + 1, unit)}（多 1）")
    return f"边界值 {_display_value(value, unit)} 缺少两侧相邻值：{'、'.join(missing)}"


# ---------------------------------------------------------------------------
# 本地规则质检（默认零 LLM 调用）
# ---------------------------------------------------------------------------


def _field(case: Mapping[str, Any], name: str) -> str:
    """统一读取字段（str 归一）。"""
    value = case.get(name, "")
    return str(value).strip() if value is not None else ""


def case_title_tags(title: str) -> list[str]:
    """用例标题中的追溯标签（[FR-x] / [NFR-x] / [FR-auto-N]，逐字保留）。"""
    return [t for t in _TAG_RE.findall(title) if _ID_LIKE_RE.match(t)]


def run_local_checks(
    cases: Sequence[Mapping[str, Any]],
    source_text: str = "",
    source_frs: Sequence[FRItem] = (),
    source_name: str = "",
) -> DoctorReport:
    """纯本地规则质检（迭代 10 / UP-1）：默认零 LLM 调用、零 token。

    检查口径（复用 core/quality.py 检查器，词表不复制）：
    - 阻断项：模糊词（含引文豁免）/ 弱断言 / 同义反复死短语 / 测量值预言 /
      断链编号（标题 [FR-x] 标签在 --source 原文编号中不存在）；
    - 警告：契约发明（code= / localStorage / 日志格式，PRD 无依据）/
      边界缺两侧相邻值 / 原文编号未被任何用例引用；
    - 提示：设计假设标注（关键词列「,假设」）、未提供 --source 的跳过说明。
    """
    rows = list(cases)
    source_pairs = quality.extract_numeric_tokens(source_text) if source_text else None
    report = DoctorReport(
        source_file="", case_count=len(rows), source_name=source_name
    )
    findings = report.findings

    for case in rows:
        title = _field(case, "title") or "（无标题）"
        expected = _field(case, "expected")
        steps = _field(case, "steps")
        method = _field(case, "method")

        fuzzy = quality.find_fuzzy_words(expected)
        if fuzzy:
            findings.append(
                DoctorFinding(
                    SEVERITY_BLOCK, CHECK_FUZZY, title, f"预期结果含模糊词「{'、'.join(fuzzy)}」",
                    "请改为明确的判定标准（具体状态 / 提示语 / 数值）；引用提示原文时用引号包裹或以「提示…」引出",
                )
            )
        weak = quality.find_weak_assertions(expected)
        if weak:
            findings.append(
                DoctorFinding(
                    SEVERITY_BLOCK, CHECK_WEAK, title, f"预期结果含弱断言「{'」、「'.join(weak)}」",
                    "弱断言无可验证行为，请写明具体验证点（状态码 / 字段值 / 提示文案 / 界面元素）",
                )
            )
        tautologies = quality.find_tautologies(expected)
        if tautologies:
            findings.append(
                DoctorFinding(
                    SEVERITY_BLOCK, CHECK_TAUTOLOGY, title,
                    f"预期结果同义反复（死短语 OR 语义）：「{'」、「'.join(tautologies)}」",
                    "预期结果只是复述需求条款、无可验证行为，请写明具体验证点（状态码 / 字段值 / 提示文案 / 界面元素）",
                )
            )
        measured = quality.find_measurement_predictions(expected, source_pairs)
        if measured:
            findings.append(
                DoctorFinding(
                    SEVERITY_BLOCK, CHECK_MEASUREMENT, title,
                    f"预期结果预言实测值「{'」、「'.join(measured)}」",
                    "性能预期只允许判定标准形态（≤2s、≥100 QPS、不超过 X），且数值须 PRD 原文有据，请改写或删除",
                )
            )
        contracts = quality.find_contract_inventions(f"{steps}\n{expected}", source_text)
        if contracts:
            findings.append(
                DoctorFinding(
                    SEVERITY_WARN, CHECK_CONTRACT, title,
                    f"PRD 无依据的契约细节「{'」、「'.join(contracts)}」",
                    "契约类细节属设计假设，请与开发确认接口契约或删除",
                )
            )
        if method == "边界值":
            missing = find_missing_adjacent(expected)
            if missing:
                findings.append(
                    DoctorFinding(
                        SEVERITY_WARN, CHECK_BOUNDARY, title, missing,
                        "边界值预期须同时给出边界值与两侧相邻值（v-1 / v+1）及各自行为（接受 / 拒绝 / 触发）",
                    )
                )
        if case.get("assumption"):
            findings.append(
                DoctorFinding(
                    SEVERITY_INFO, CHECK_ASSUMPTION, title,
                    "关键词列标注「,假设」（衍生 / 设计假设类用例）",
                    "请与开发确认接口契约，确认后保留或删除该用例",
                )
            )

    # 编号引用一致性（[FR-x] 标签与 --source 原文编号逐字比对）
    source_ids = [fr.id for fr in source_frs]
    if not source_text:
        findings.append(
            DoctorFinding(
                SEVERITY_INFO, CHECK_NUMBERING, "（全部用例）",
                "未提供 --source，无法比对 [FR-x] 原文编号",
                "补充 --source <prd.md> 后复检编号引用一致性",
            )
        )
    elif not source_ids:
        findings.append(
            DoctorFinding(
                SEVERITY_INFO, CHECK_NUMBERING, "（全部用例）",
                "PRD 原文未识别到需求编号，无可比对的原文编号",
                "如需编号追溯，请在 PRD 中使用 FR-3 / FR-3.1 / NFR-1.2 等原文编号",
            )
        )
    else:
        id_set = set(source_ids)
        referenced: set[str] = set()
        for case in rows:
            title = _field(case, "title") or "（无标题）"
            for tag in case_title_tags(title):
                if tag in id_set:
                    referenced.add(tag)
                else:
                    findings.append(
                        DoctorFinding(
                            SEVERITY_BLOCK, CHECK_NUMBERING, title,
                            f"标题追溯标签 [{tag}] 在 PRD 原文编号中不存在（断链编号）",
                            f"请把标签改为原文编号之一（{'、'.join(source_ids)}），或补齐 PRD 对应条款",
                        )
                    )
        for fr_id in source_ids:
            if fr_id not in referenced:
                findings.append(
                    DoctorFinding(
                        SEVERITY_WARN, CHECK_NUMBERING, f"PRD 编号 {fr_id}",
                        f"原文编号 {fr_id} 未被任何用例标题 [编号] 引用",
                        f"请补充覆盖 {fr_id} 的用例（标题带 [{fr_id}] 追溯前缀），或确认该编号无需用例覆盖",
                    )
                )
    return report


# ---------------------------------------------------------------------------
# LLM 复核（--llm-review）：只复核本地可疑条目，结论合并进报告
# ---------------------------------------------------------------------------

DOCTOR_REVIEW_SYSTEM_PROMPT = (
    "你是资深测试用例质检复核专家。本地规则质检给出了一批可疑条目，"
    "请逐条判断是「确认」（确属质量问题）还是「误报」（引用原文 / 合法表述被误判），"
    "并给出一句话中文理由。只输出一个合法 JSON 对象，不要任何解释文字或 markdown 代码块，结构为：\n"
    '{"reviews": [{"n": 1, "verdict": "确认", "comment": "一句话理由"}]}'
)


def parse_review_json(content: str) -> list[dict]:
    """从 LLM 输出解析复核 JSON（容忍 ```json 代码块），失败抛清晰中文错误。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 复核输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("reviews"), list):
        raise ValueError("LLM 复核输出应为含 reviews 数组的 JSON 对象")
    return data["reviews"]


def llm_review_findings(findings: Sequence[DoctorFinding]) -> list[str]:
    """把本地可疑条目送 LLM 复核，返回中文复核结论列表（合并进报告）。"""
    lines = ["请复核以下本地质检可疑条目并输出 JSON："]
    for n, item in enumerate(findings, 1):
        lines.append(f"{n}. 用例「{item.title}」（{item.check}）命中「{item.hit}」")
    content = llm_gateway.call_chat_completions(
        llm_gateway.get_base_url("openai"),
        llm_gateway.get_api_key(),
        llm_gateway.get_model("openai"),
        DOCTOR_REVIEW_SYSTEM_PROMPT,
        "\n".join(lines),
    )
    conclusions: list[str] = []
    for item in parse_review_json(content):
        if not isinstance(item, dict):
            continue
        n = item.get("n")
        title = "（未知条目）"
        if isinstance(n, int) and 1 <= n <= len(findings):
            title = findings[n - 1].title
        verdict = str(item.get("verdict", "")).strip() or "未给出结论"
        comment = str(item.get("comment", "")).strip() or "无补充说明"
        conclusions.append(f"可疑条目「{title}」复核结论：{verdict}（{comment}）")
    return conclusions or ["LLM 复核未返回有效结论，请结合本地质检明细人工复核"]


def run_llm_review(findings: Sequence[DoctorFinding]) -> list[str]:
    """--llm-review 入口：有 API key 才复核，无 key 跳过并中文提示；异常不阻断本地结论。"""
    if not llm_gateway.get_api_key():
        return [
            "未配置 API key（CASEPILOT_LLM_API_KEY / OPENAI_API_KEY），"
            "--llm-review 已跳过，本次为纯本地质检结论（零 token 消耗）"
        ]
    if not findings:
        return ["本地质检无疑似条目（无阻断项与警告），无需 LLM 复核"]
    try:
        return llm_review_findings(findings)
    except Exception as exc:  # noqa: BLE001 - 网络 / 解析失败不阻断本地质检结论
        return [f"LLM 复核失败：{exc}（本地质检结论不受影响）"]
