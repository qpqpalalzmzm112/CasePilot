"""中文 PRD 文本解析：提取功能点、数字边界、主流程与 FR 清单。

解析策略（确定性规则，不依赖任何外部服务）：
- 功能点：功能清单 / 特性类小节下的列表项，或「支持 / 允许 / 提供 …」动宾短语句
- 数字边界：带单位的数字（8-20 位、5 次、30 分钟）与比较词（至少 / 不超过 / 超过 / 满 等）
- 主流程：「主流程 / 基本流 / 核心流程」小节下的编号步骤；缺失时按功能点合成
- FR 清单（迭代 6 需求对账）：优先识别显式编号（FR-1.1 / 1.2 / 需求1 等形式），
  无显式编号时按功能点自动编号（FR-1、FR-2…）；同时记录条目所在小节，
  供安全 / 性能类需求的语境判定使用
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

# 显式 FR 编号（迭代 6 需求对账）：FR-1.1 / 需求1 / R2 等前缀形式
_EXPLICIT_FR_PREFIX_RE = re.compile(r"^(?:FR|需求|R)[-\s]?(\d+(?:\.\d+)*)[：:、.．\s]+(.+)$")
# 显式 FR 编号：1.2 / 3.1.4 等至少两级的层级编号形式（"1. 步骤"类一级编号不算）
_EXPLICIT_FR_HIER_RE = re.compile(r"^(\d+\.\d+(?:\.\d+)*)[：:、.．\s]+(.+)$")


def _match_explicit_fr(text: str) -> re.Match | None:
    """识别显式 FR 编号条目，返回（编号, 需求文本）的匹配；无编号返回 None。"""
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

    # FR 清单兜底（迭代 6 需求对账）：无显式编号时按功能点自动编号 FR-1、FR-2…
    if not fr_items:
        fr_items = [FRItem(id=str(i), text=f.name, section=f.section) for i, f in enumerate(features, 1)]

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
