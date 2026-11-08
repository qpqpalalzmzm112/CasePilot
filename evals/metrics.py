"""4 维评测评分器（全部可离线计算，结果确定性）。

评分维度（每维 0-1，等权平均 × 100 为总分，口径 0-100）：
1. 结构合法率：生成用例通过 pydantic TestCase 校验的比例
2. 方法论覆盖率：golden 基准集要求的 method 被生成集覆盖的比例
3. 边界数值化率：method=边界值 的用例 expected 含具体数字的比例
   （生成集无边界值用例时记 0 分：数值化率以「应产出边界值用例」为前提）
4. 基准要素重合度：golden 用例「模块 + method + category」三元组被生成集
   宽松匹配的比例（模块按父模块路径 / 包含关系宽松比对）

golden set 格式：JSON 数组，元素为 TestCase 八要素字段 + 可选 note（设计理由，中文）。
全部输出中文；本模块不做任何网络访问。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, Union

from casepilot.core.schema import METHODS, TestCase

# golden 基准集默认目录（与本包同级的 datasets/）
DATASETS_DIR = Path(__file__).resolve().parent / "datasets"

# 具体数字判定：expected 中出现阿拉伯数字即视为数值化
_NUMBER_RE = re.compile(r"\d")

# 评分维度固定名称（报告与测试共用）
DIM_STRUCTURE = "结构合法率"
DIM_METHOD = "方法论覆盖率"
DIM_BOUNDARY = "边界数值化率"
DIM_OVERLAP = "基准要素重合度"
DIMENSIONS: tuple[str, ...] = (DIM_STRUCTURE, DIM_METHOD, DIM_BOUNDARY, DIM_OVERLAP)


@dataclass(frozen=True)
class GoldenCase:
    """golden 基准用例：TestCase 八要素 + 可选设计理由（note）。"""

    case: TestCase
    note: str = ""


def load_golden(path: Union[str, Path]) -> list[GoldenCase]:
    """加载并校验 golden 基准集，失败抛出清晰中文错误。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"基准集文件不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"基准集不是合法 JSON：{p}（{exc}）") from exc
    if not isinstance(data, list) or not data:
        raise ValueError(f"基准集应为非空 JSON 数组（元素为用例对象）：{p}")

    golden: list[GoldenCase] = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"基准集第 {i} 条应为 JSON 对象（用例字段），实际为 {type(item).__name__}")
        raw = dict(item)
        note = str(raw.pop("note", "") or "")
        try:
            case = TestCase.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - 统一转中文错误
            raise ValueError(f"基准集第 {i} 条用例结构校验失败：{exc}") from exc
        golden.append(GoldenCase(case=case, note=note))
    return golden


def find_golden_path(
    input_path: Union[str, Path], datasets_dir: Union[str, Path, None] = None
) -> Path:
    """按输入文档名自动匹配 golden：datasets/<输入名>_golden.json。"""
    stem = Path(input_path).stem
    base = Path(datasets_dir) if datasets_dir is not None else DATASETS_DIR
    candidate = base / f"{stem}_golden.json"
    if not candidate.is_file():
        raise FileNotFoundError(
            f"未找到基准集：{candidate}（可用 --golden 显式指定，"
            f"或在 {base} 下放置 <输入名>_golden.json）"
        )
    return candidate


@dataclass
class DimensionScore:
    """单个评分维度的结果（得分 0-1 + 一句话解读 + 差距明细）。"""

    name: str
    score: float
    summary: str
    gaps: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    """4 维评测结果（确定性，可渲染为中文 Markdown 报告）。"""

    dimensions: list[DimensionScore]

    @property
    def total(self) -> float:
        """总分（0-100）：4 个维度等权平均 × 100，保留 1 位小数。"""
        avg = sum(d.score for d in self.dimensions) / len(self.dimensions)
        return round(avg * 100, 1)

    @property
    def suggestions(self) -> list[str]:
        """改进建议：由各维度差距明细生成（引用具体用例标题）。"""
        items: list[str] = []
        for dim in self.dimensions:
            items.extend(dim.gaps)
        return items

    def format_report(
        self,
        *,
        input_name: str,
        golden_name: str,
        golden_count: int,
        case_count: int,
        mode_note: str = "",
    ) -> str:
        """渲染全中文 Markdown 评分报告（确定性输出，不含时间戳）。"""
        lines = [
            f"# 评测报告：{input_name}",
            "",
            f"- 输入文档：{input_name}",
            f"- 基准集：{golden_name}（{golden_count} 条人工整理基准用例）",
            f"- 生成用例：{case_count} 条" + (f"（{mode_note}）" if mode_note else ""),
            f"- **总分：{self.total} / 100**（口径：4 个维度等权平均 × 100，各维度 0-1）",
            "",
            "## 维度得分",
            "",
            "| 维度 | 得分 | 一句话解读 |",
            "| --- | --- | --- |",
        ]
        for dim in self.dimensions:
            lines.append(f"| {dim.name} | {dim.score * 100:.1f}% | {dim.summary} |")
        lines.append("")
        lines.append("## 改进建议")
        lines.append("")
        if self.suggestions:
            for i, s in enumerate(self.suggestions, 1):
                lines.append(f"{i}. {s}")
        else:
            lines.append("无：4 个维度全部满分，生成集与基准集完全对齐。")
        return "\n".join(lines)


def _as_testcase(item: Union[TestCase, Mapping[str, Any]]) -> Union[TestCase, None]:
    """尝试把生成用例（TestCase 或原始 dict）校验为 TestCase，失败返回 None。"""
    if isinstance(item, TestCase):
        return item
    try:
        return TestCase.model_validate(dict(item))
    except Exception:  # noqa: BLE001 - 校验失败即计入结构非法
        return None


def _module_parent(module: str) -> str:
    """取模块路径的父模块（第一段），用于宽松匹配。"""
    return module.split("/")[0].strip()


def _module_match(a: str, b: str) -> bool:
    """模块宽松匹配：父模块相同，或任一模块包含另一方父模块。"""
    pa, pb = _module_parent(a), _module_parent(b)
    if not pa or not pb:
        return False
    return pa == pb or pa in b or pb in a


def structure_validity_rate(raw_cases: Sequence[Any]) -> DimensionScore:
    """维度 1：结构合法率 = 通过 pydantic 校验的用例数 / 总数。"""
    total = len(raw_cases)
    if total == 0:
        return DimensionScore(DIM_STRUCTURE, 0.0, "生成用例为空，无法计算结构合法率", ["生成集为空，请检查生成流程"])
    invalid_titles: list[str] = []
    valid = 0
    for item in raw_cases:
        if _as_testcase(item) is not None:
            valid += 1
        else:
            title = str(item.get("title", "（无标题）")) if isinstance(item, Mapping) else "（无标题）"
            invalid_titles.append(title)
    rate = valid / total
    gaps = [
        f"用例「{t}」未通过 pydantic 结构校验（八要素缺失 / 枚举非法 / 未知字段），请修复该用例结构"
        for t in invalid_titles[:5]
    ]
    return DimensionScore(
        DIM_STRUCTURE,
        rate,
        f"{valid}/{total} 条用例通过 pydantic 结构校验",
        gaps,
    )


def method_coverage_rate(
    golden: Sequence[GoldenCase], generated: Sequence[Any]
) -> DimensionScore:
    """维度 2：方法论覆盖率 = golden 要求的 method 被生成集覆盖的比例。"""
    required = sorted({g.case.method for g in golden}, key=lambda m: METHODS.index(m) if m in METHODS else 99)
    if not required:
        return DimensionScore(DIM_METHOD, 1.0, "基准集未要求特定方法论", [])
    used = set()
    for item in generated:
        case = _as_testcase(item)
        if case is not None:
            used.add(case.method)
    missing = [m for m in required if m not in used]
    rate = (len(required) - len(missing)) / len(required)
    gaps = []
    for m in missing:
        ref = next((g.case.title for g in golden if g.case.method == m), "")
        gaps.append(
            f"生成集缺少「{m}」方法论用例（基准用例「{ref}」要求），建议按{m}方法论补充对应场景"
        )
    return DimensionScore(
        DIM_METHOD,
        rate,
        f"基准要求 {len(required)} 种方法论，生成集覆盖 {len(required) - len(missing)} 种"
        + (f"，缺少：{'、'.join(missing)}" if missing else ""),
        gaps,
    )


def boundary_numeric_rate(generated: Sequence[Any]) -> DimensionScore:
    """维度 3：边界数值化率 = 边界值用例 expected 含具体数字的比例。"""
    boundary_cases: list[TestCase] = []
    for item in generated:
        case = _as_testcase(item)
        if case is not None and case.method == "边界值":
            boundary_cases.append(case)
    if not boundary_cases:
        return DimensionScore(
            DIM_BOUNDARY,
            0.0,
            "生成集没有边界值用例，边界数值化率记 0 分",
            ["生成集没有「边界值」用例，请针对需求中的数字边界补充边界值用例"],
        )
    concrete = [c for c in boundary_cases if _NUMBER_RE.search(c.expected)]
    rate = len(concrete) / len(boundary_cases)
    gaps = [
        f"边界值用例「{c.title}」的预期结果未出现具体数值，请写明边界值与两侧相邻值的期望行为"
        for c in boundary_cases
        if c not in concrete
    ][:5]
    return DimensionScore(
        DIM_BOUNDARY,
        rate,
        f"{len(concrete)}/{len(boundary_cases)} 条边界值用例的预期结果含具体数值",
        gaps,
    )


def golden_overlap_rate(
    golden: Sequence[GoldenCase], generated: Sequence[Any]
) -> DimensionScore:
    """维度 4：基准要素重合度 = golden「模块+method+category」三元组被宽松匹配的比例。"""
    if not golden:
        return DimensionScore(DIM_OVERLAP, 1.0, "基准集为空，无三元组可比对", [])
    gen_cases = [c for c in (_as_testcase(i) for i in generated) if c is not None]
    unmatched: list[str] = []
    matched = 0
    for g in golden:
        hit = any(
            c.method == g.case.method
            and c.category == g.case.category
            and _module_match(g.case.module, c.module)
            for c in gen_cases
        )
        if hit:
            matched += 1
        else:
            unmatched.append(g.case.title)
    rate = matched / len(golden)
    gaps = [
        f"基准用例「{t}」（模块+方法论+类型三元组）未被生成集覆盖，请检查该场景是否遗漏"
        for t in unmatched[:5]
    ]
    return DimensionScore(
        DIM_OVERLAP,
        rate,
        f"{matched}/{len(golden)} 条基准用例的「模块+方法论+类型」三元组被生成集覆盖",
        gaps,
    )


def evaluate(generated: Sequence[Any], golden: Sequence[GoldenCase]) -> EvalReport:
    """对生成用例集执行 4 维评分，返回确定性评测结果。"""
    dimensions = [
        structure_validity_rate(generated),
        method_coverage_rate(golden, generated),
        boundary_numeric_rate(generated),
        golden_overlap_rate(golden, generated),
    ]
    return EvalReport(dimensions=dimensions)
