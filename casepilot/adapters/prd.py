"""中文 PRD 文本解析：提取功能点、数字边界、主流程与 FR/NFR 需求清单。

解析策略（确定性规则，不依赖任何外部服务）：
- 功能点：功能清单 / 特性类小节下的列表项，或「支持 / 允许 / 提供 …」动宾短语句
- 数字边界：带单位的数字（8-20 位、5 次、30 分钟）与比较词（至少 / 不超过 / 超过 / 满 等）
- 主流程：「主流程 / 基本流 / 核心流程」小节下的编号步骤；缺失时按功能点合成
- FR/NFR 需求清单（迭代 7 对账返工，迭代 8 抽取鲁棒化 + 硬门禁）：抽取「带原始编号
  的需求清单」——识别 FR/NFR 分层编号（FR-3 / FR-4.3 / FR-1.3.2 / NFR-1.2 / 需求1 /
  需求 2 / R2 / 1.4 等形式，含中英文冒号 / 顿号 / 括号 / 加粗 / 表格单元格上下文与
  全角连字符 FR—3），编号逐字保留原文（大小写、分隔符不改写），每条记录原文条款文本；
  文档能侦测到编号样式但抽取 0 条时抛中文异常（禁止静默兜底）；仅当文档完全无编号
  样式时才允许自动编号兜底（FR-auto-1…，对账报告注明「自动编号」）。条目所在小节
  同步记录，供安全 / 性能类需求的语境判定使用
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.schema import Boundary, DocumentProfile, Feature, FRItem

# 数字单位（中文需求文档常见量词，含性能指标常见单位）
_UNIT_PATTERN = r"位|次|分钟|小时|秒|天|件|元|个|岁|字符|张|条|笔|分|%|％|并发|QPS|qps"
# 区间：如 8-20 位 / 8~20 位 / 8 至 20 位
_RANGE_RE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*[-~—～至]\s*(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})"
)
# 比较词 + 数字（±单位）：如 满 100 减 20 / 至少 1 件 / 不超过 50 件 / 低于 100 元
_KEYWORD_RE = re.compile(
    r"(满|减|加|打|折|最少|至少|不少于|不低于|最低|最多|不超过|不高于|最高|超过|大于|小于|低于|高于|≥|≤)"
    rf"\s*(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})?"
)
# 单个数字 + 单位：如 5 次 / 30 分钟 / 100 元
_NUMBER_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*({_UNIT_PATTERN})")
# 标题行
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
# 列表项（- / * / 1. / 1、）
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+(.+?)\s*$")
# 功能点 / 特性类小节（含安全 / 性能类非功能小节，迭代 6 方法论标注校准）
_FEATURE_SECTION_RE = re.compile(r"功能|特性|能力|需求点|支持|安全|性能|非功能|指标")
# 流程类小节
_FLOW_SECTION_RE = re.compile(r"主流程|基本流|核心流程|操作流程|业务流程")
# 动宾短语兜底识别
_ACTION_LINE_RE = re.compile(r"^\s*(支持|允许|提供|可以|能够|用户可以)")

# 显式需求编号（迭代 7 对账返工 + 迭代 8 抽取鲁棒化）：
# 编号形式覆盖 FR-3（单级）/ FR-4.3 / FR-1.3.2（多级）/ NFR-1.2 / 需求1 / 需求 2 / R2 / 1.4，
# 容忍全角 / 半角连字符（FR—3）、全角半角冒号、顿号、括号包裹（（FR-3））、
# Markdown 加粗（**FR-3**）与表格单元格上下文；编号逐字保留原文（大小写、分隔符不改写），
# 每条记录原文条款文本。
# 编号核心（逐字保留，不改写大小写与分隔符）
_ID_CORE = r"(?:NFR|FR|需求|R)[—–\-－\s]?\d+(?:\.\d+)*"
# 多级层级编号核心：1.2 / 3.1.4（"1. 步骤"类一级编号不算）
_HIER_CORE = r"\d+\.\d+(?:\.\d+)*"
# 编号前装饰（加粗 / 反引号 / 括号 / 引用 / 表格分隔 / 列表符）
_DECOR_PRE = r"[（(【\[>*_`«»\"'、,，\-—–]*"
# 编号后装饰（加粗 / 反引号，闭括号交由分隔符消费）
_DECOR_POST = r"[>*_`«»\"']*"
# 编号与条款文本的分隔：全角半角冒号 / 顿号 / 句点 / 闭括号 / 空白
_FR_SEP = r"(?:[：:、.．）)】\]]|\s)+"
_EXPLICIT_FR_PREFIX_RE = re.compile(
    rf"^\s*{_DECOR_PRE}({_ID_CORE}){_DECOR_POST}{_FR_SEP}(.+)$", re.IGNORECASE
)
# 显式需求编号：1.2 / 3.1.4 等至少两级的层级编号形式（"1. 步骤"类一级编号不算）
_EXPLICIT_FR_HIER_RE = re.compile(rf"^\s*{_DECOR_PRE}({_HIER_CORE}){_DECOR_POST}{_FR_SEP}(.+)$")

# 表格单元格整格编号（如 | FR-3 | 支持忘记密码 | 的 FR-3 格）
_TABLE_CELL_ID_RE = re.compile(
    rf"^\s*{_DECOR_PRE}({_ID_CORE}){_DECOR_POST}\s*$", re.IGNORECASE
)
# 表格分隔 / 占位格（| --- | 、| — |），不作条款文本
_TABLE_PLACEHOLDER_RE = re.compile(r"^[—\-–—\s:：]+$")

# ---------------------------------------------------------------------------
# 编号样式侦测（迭代 8 / UP-1 硬门禁）：识别「文档使用原文编号」的痕迹。
# FR / NFR 编号（容忍全角连字符）出现在任意位置均算 1 处；
# 需求N / R# / 多级数字编号按行首（容忍加粗 / 括号 / 表格格等装饰）侦测，
# 避免把正文里的「用户需求 3 天」误判为编号样式。
# ---------------------------------------------------------------------------
_FR_TOKEN_ANYWHERE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:NFR|FR)[—–\-－]?\d+(?:\.\d+)*", re.IGNORECASE
)
_LINE_PREFIX_TOKEN_RE = re.compile(
    rf"^\s*{_DECOR_PRE}(?:(?:需求|R)[—–\-－\s]?\d+(?:\.\d+)*|{_HIER_CORE})", re.IGNORECASE
)


def _count_numbering_styles(text: str) -> int:
    """统计文档中原文编号样式出现的处数（硬门禁报错用，全角连字符同样计入）。"""
    count = len(_FR_TOKEN_ANYWHERE_RE.findall(text))
    for raw in text.splitlines():
        for cell in raw.split("|") + raw.split("｜"):
            if _LINE_PREFIX_TOKEN_RE.match(cell):
                count += 1
    return count


def _match_explicit_fr(text: str) -> re.Match | None:
    """识别显式需求编号条目，返回（原文编号, 需求文本）的匹配；无编号返回 None。

    编号（group 1）逐字保留原文，不做任何大小写 / 分隔符改写（迭代 7）；
    容忍加粗 / 括号 / 引用等装饰与全角连字符（迭代 8 鲁棒化）。
    """
    return _EXPLICIT_FR_PREFIX_RE.match(text) or _EXPLICIT_FR_HIER_RE.match(text)


def _snippet(line: str, start: int, end: int, keep: int = 12) -> str:
    """截取数字匹配处的上下文片段，作为边界描述（确定性截断）。"""
    begin = max(0, start - keep)
    frag = line[begin:end].strip()
    # 去掉片段开头的残留标点
    return frag.lstrip("，,。.：:；;、（(【[").strip()


def _extract_boundaries(line: str) -> list[Boundary]:
    """从单行文本中提取数字边界（区间 → 比较词 → 单值，避免重复计数）。"""
    boundaries: list[Boundary] = []
    occupied: list[tuple[int, int]] = []

    def _free(pos: int) -> bool:
        return all(pos < s or pos >= e for s, e in occupied)

    # 1) 区间边界：如 8-20 位
    for m in _RANGE_RE.finditer(line):
        low, high, unit = m.group(1), m.group(2), m.group(3)
        text = _snippet(line, m.start(), m.end())
        boundaries.append(Boundary(text=text, value=float(low), kind="range_min", unit=unit))
        boundaries.append(Boundary(text=text, value=float(high), kind="range_max", unit=unit))
        occupied.append((m.start(), m.end()))

    # 2) 比较词 + 数字：如 满 100 减 20 / 不超过 50 件
    for m in _KEYWORD_RE.finditer(line):
        if not _free(m.start()):
            continue
        text = _snippet(line, m.start(), m.end())
        boundaries.append(
            Boundary(text=text, value=float(m.group(2)), kind="threshold", unit=m.group(3) or "")
        )
        occupied.append((m.start(), m.end()))

    # 3) 单个数字 + 单位：如 5 次 / 30 分钟
    for m in _NUMBER_RE.finditer(line):
        if not _free(m.start()):
            continue
        text = _snippet(line, m.start(), m.end())
        boundaries.append(
            Boundary(text=text, value=float(m.group(1)), kind="threshold", unit=m.group(2))
        )
    return boundaries


def _clean_item(text: str) -> str:
    """清洗列表项 / 步骤文本：去尾部标点。"""
    return text.strip().rstrip("。．.;；")


def parse_text(text: str, source: str = "文本输入") -> DocumentProfile:
    """把中文 PRD 文本解析为 DocumentProfile（含 FR 清单）。"""
    default_module = Path(source).stem if source else "需求文档"
    module = default_module
    lines = text.splitlines()

    features: list[Feature] = []
    boundaries: list[Boundary] = []
    flows: list[str] = []
    fr_items: list[FRItem] = []
    has_heading = False
    in_feature_section = False
    in_flow_section = False
    current_section = ""

    for line in lines:
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            has_heading = True
            title = heading_match.group(2)
            # 第一个一级 / 二级标题作为文档主模块名
            if module == default_module and len(heading_match.group(1)) <= 2 and title:
                module = _clean_item(title) or module
            current_section = _clean_item(title)
            in_feature_section = bool(_FEATURE_SECTION_RE.search(title))
            in_flow_section = bool(_FLOW_SECTION_RE.search(title))
            continue

        # 数字边界：每一行都扫描（含标题外的正文、列表、步骤行），并记录所在小节
        for b in _extract_boundaries(line):
            boundaries.append(b.model_copy(update={"section": current_section}))

        stripped_line = line.strip()

        # 表格行（迭代 8 鲁棒化）：| FR-3 | 支持忘记密码 | 单元格内编号逐字抽取
        if stripped_line.startswith(("|", "｜")):
            cells = [c.strip() for c in re.split(r"[|｜]", stripped_line) if c.strip()]
            id_idx = None
            fr_id = ""
            for idx, cell in enumerate(cells):
                cell_match = _TABLE_CELL_ID_RE.match(cell)
                if cell_match:
                    id_idx, fr_id = idx, cell_match.group(1)
                    break
            if id_idx is not None:
                text_cells = [
                    _clean_item(c)
                    for j, c in enumerate(cells)
                    if j != id_idx and not _TABLE_PLACEHOLDER_RE.match(c)
                ]
                fr_text = "，".join(t for t in text_cells if t)
                if fr_text:
                    if all(fr_id != f.id for f in fr_items):
                        fr_items.append(FRItem(id=fr_id, text=fr_text, section=current_section))
                    if all(fr_text != f.name for f in features):
                        features.append(Feature(name=fr_text, description="", section=current_section))
            continue

        bullet_match = _BULLET_RE.match(line)
        item = _clean_item(bullet_match.group(1)) if bullet_match else None
        candidate = item if item is not None else _clean_item(line.strip())

        # 显式 FR 编号条目（FR-1.1 / 1.2 / 需求1 等）：优先于自动编号，
        # 同时登记为功能点，让规则引擎为该需求生成用例
        fr_match = _match_explicit_fr(candidate) if candidate else None
        if fr_match:
            fr_id, fr_text = fr_match.group(1), _clean_item(fr_match.group(2))
            if fr_text:
                if all(fr_id != f.id for f in fr_items):
                    fr_items.append(FRItem(id=fr_id, text=fr_text, section=current_section))
                if all(fr_text != f.name for f in features):
                    features.append(Feature(name=fr_text, description="", section=current_section))
            continue

        if bullet_match:
            if not item:
                continue
            if in_flow_section:
                flows.append(item)
            elif in_feature_section or not has_heading:
                features.append(Feature(name=item, description="", section=current_section))
            continue

        # 流程小节中不带编号的步骤行（如「打开登录页面」）
        stripped = line.strip()
        if in_flow_section and stripped and len(stripped) <= 30:
            flows.append(_clean_item(stripped))
            continue

        # 动宾短语兜底识别为功能点
        if _ACTION_LINE_RE.match(stripped):
            item = _clean_item(stripped)
            if item and all(item != f.name for f in features):
                features.append(Feature(name=item, description="", section=current_section))

    # 去重（保持出现顺序）
    seen: set[str] = set()
    deduped: list[Feature] = []
    for f in features:
        if f.name not in seen:
            seen.add(f.name)
            deduped.append(f)
    features = deduped

    # 边界去重：同值 / 同类型 / 同单位视为同一边界（保持出现顺序）
    seen_b: set[tuple[float, str, str]] = set()
    deduped_b: list[Boundary] = []
    for b in boundaries:
        key = (b.value, b.kind, b.unit)
        if key not in seen_b:
            seen_b.add(key)
            deduped_b.append(b)
    boundaries = deduped_b

    # 需求清单兜底（迭代 7 对账返工 + 迭代 8 硬门禁）：
    # 文档能侦测到原文编号样式但抽取 0 条 → 抛中文异常（禁止静默回退自动编号），
    # 由 CLI 以 exit 1 退出；仅当文档完全无编号样式时才允许自动编号兜底，
    # 编号为 FR-auto-1…（auto=True），对账报告中注明「自动编号」
    if not fr_items:
        detected = _count_numbering_styles(text)
        if detected:
            raise ValueError(
                f"检测到原文编号样式 {detected} 处但抽取 0 条需求条目，已禁止静默回退自动编号："
                "请检查需求编号书写形式（支持 FR-3 / FR-4.3 / FR-1.3.2 / NFR-1.2 / 需求1 / 需求 2 / R2 / 1.4，"
                "容忍全角连字符 FR—3、全角半角冒号、顿号、括号（FR-3）、加粗 **FR-3** 与表格单元格上下文）"
            )
        fr_items = [
            FRItem(id=f"FR-auto-{i}", text=f.name, section=f.section, auto=True)
            for i, f in enumerate(features, 1)
        ]

    # 主流程缺失时，按功能点合成（保证场景法用例可生成）
    if not flows:
        if features:
            flows = [f"完成「{_truncate(f.name)}」" for f in features[:3]]
        else:
            flows = [f"完成「{module}」的核心操作"]

    return DocumentProfile(
        source=source,
        source_type="prd",
        module=module or "需求文档",
        features=features,
        boundaries=boundaries,
        flows=flows,
        frs=fr_items,
    )


def _truncate(text: str, limit: int = 16) -> str:
    """确定性截断长文本，避免用例标题过长。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"
