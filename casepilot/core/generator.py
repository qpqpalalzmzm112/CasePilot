"""生成编排：输入文档 → 解析 → LLM 网关生成 → pydantic 校验 → 质量门禁 → GenerationResult。

迭代 6 新增需求对账机制（UP-1）：生成后把用例逐条对账到 FR 清单——
每个 FR 至少 1 条用例（覆盖用例标题带 [FR-x.y] 追溯前缀），缺漏的 FR 自动生成
基础用例补齐（有效类 + 异常类各 1 条，method 按 FR 内容选方法论），
并输出中文对账报告（随生成流程打印，落盘 output/<输入名>_fr_coverage.md）。
迭代 8 对账硬门禁（UP-1）：覆盖率强制 100%——补齐后仍缺任一 FR/NFR 即抛中文
异常使生成失败（exit 1）；覆盖率核对表升级为「PRD 条款 → 用例编号」映射
（编号 | 原文摘要 | 覆盖用例编号列表 | ✓/已补齐）。
迭代 10 / UP-2 解析缓存 + 增量生成：PRD 解析结果按输入内容 hash 落盘
output/.cache/<hash>.json，输入未变时跳过重新解析（日志明示「读取解析缓存」），
输入变更自动失效；`--only FR-3`（多值逗号分隔）只重新生成指定 FR 归属用例，
与已有用例集（上次生成结果缓存 <hash>_cases.json）合并后整体导出。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Union

from ..adapters import openapi as openapi_adapter
from ..adapters import prd as prd_adapter
from . import mock_engine
from . import quality
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
# 解析缓存 + 用例集缓存（迭代 10 / UP-2）：output/.cache/ 下按输入内容 hash 落盘
# ---------------------------------------------------------------------------

# 缓存目录（output/ 已被 .gitignore 忽略，缓存与增量产物均不入库）
CACHE_DIR = Path(FR_REPORT_DIR) / ".cache"
# 解析缓存文件名 = <输入文件内容 hash>.json（迭代 10 / UP-2）
PARSE_CACHE_SUFFIX = ".json"
# 用例集缓存（上次生成结果缓存，增量生成的合并基线）= <hash>_cases.json
CASES_CACHE_SUFFIX = "_cases.json"
# 缓存结构版本（结构变更时递增，旧缓存自动失效）
CACHE_VERSION = 1


def input_content_hash(path: Union[str, Path]) -> str:
    """输入文件内容的 sha256 哈希：解析缓存 / 用例集缓存的键，输入变更自动失效。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _cache_file(digest: str, suffix: str) -> Path:
    """按内容 hash 拼缓存文件路径。"""
    return CACHE_DIR / f"{digest}{suffix}"


def _read_cache_json(cache: Path, digest: str) -> Optional[dict]:
    """读取缓存 JSON 并校验版本与内容 hash；不合法（损坏 / hash 不匹配）返回 None。"""
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != CACHE_VERSION or data.get("source_hash") != digest:
        return None
    return data


def load_parse_cache(digest: str) -> Optional[DocumentProfile]:
    """读取解析缓存（输入内容未变时跳过重新解析）；无有效缓存返回 None。"""
    cache = _cache_file(digest, PARSE_CACHE_SUFFIX)
    if not cache.is_file():
        logger.info("解析缓存未命中，执行解析：%s", cache)
        return None
    data = _read_cache_json(cache, digest)
    if data is None:
        logger.info("解析缓存失效（内容 hash 不匹配或缓存损坏），重新解析：%s", cache)
        return None
    try:
        profile = DocumentProfile.model_validate(data["profile"])
    except Exception:  # noqa: BLE001 - 缓存结构损坏统一按失效处理
        logger.info("解析缓存失效（缓存结构损坏），重新解析：%s", cache)
        return None
    logger.info("读取解析缓存：%s（输入内容未变更，跳过重新解析）", cache)
    return profile


def save_parse_cache(digest: str, profile: DocumentProfile) -> Path:
    """把解析结果落盘 JSON（output/.cache/<输入文件内容 hash>.json）。"""
    cache = _cache_file(digest, PARSE_CACHE_SUFFIX)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {"version": CACHE_VERSION, "source_hash": digest, "profile": profile.model_dump(mode="json")},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return cache


def parse_document_cached(path: Union[str, Path], source_type: Optional[SourceType] = None) -> DocumentProfile:
    """解析输入文档（带解析缓存，迭代 10 / UP-2）。

    输入内容未变时直接读 output/.cache/<内容 hash>.json（日志明示「读取解析缓存」）；
    输入变更（内容 hash 不匹配）自动失效并重新解析、刷新缓存。
    """
    p = Path(path)
    digest = input_content_hash(p)
    profile = load_parse_cache(digest)
    if profile is not None:
        return profile
    profile = parse_document(p, source_type)
    save_parse_cache(digest, profile)
    return profile


def load_case_cache(digest: str) -> Optional[list[TestCase]]:
    """读取上次生成结果缓存（增量生成的合并基线）；无有效缓存返回 None。"""
    cache = _cache_file(digest, CASES_CACHE_SUFFIX)
    if not cache.is_file():
        return None
    data = _read_cache_json(cache, digest)
    if data is None:
        logger.info("用例集缓存失效（内容 hash 不匹配或缓存损坏），按无缓存处理：%s", cache)
        return None
    try:
        return [TestCase.model_validate(c) for c in data["cases"]]
    except Exception:  # noqa: BLE001 - 缓存结构损坏统一按失效处理
        logger.info("用例集缓存失效（缓存结构损坏），按无缓存处理：%s", cache)
        return None


def save_case_cache(digest: str, cases: Sequence[TestCase]) -> Path:
    """把生成 / 合并后的用例集落盘（上次生成结果缓存，供 --only 增量合并）。"""
    cache = _cache_file(digest, CASES_CACHE_SUFFIX)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "source_hash": digest,
                "cases": [c.model_dump(mode="json") for c in cases],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return cache


# ---------------------------------------------------------------------------
# FR 需求对账（迭代 6 / UP-1，迭代 9 根治）：逐条覆盖核对 + 具体化补齐 + 未覆盖清单
# ---------------------------------------------------------------------------

@dataclass
class FRCoverageItem:
    """单条 FR 的覆盖核对结果（迭代 9 三态：✓ 质量覆盖 / ✓ 已补齐 / ⚠ 未覆盖）。"""

    fr_id: str
    summary: str  # 原文摘要（条款核心短语，不带省略号）
    count: int  # 覆盖用例数（含具体化补齐用例）
    filled: bool  # True=规则生成未命中，已具体化补齐
    case_numbers: list[int] = field(default_factory=list)  # 覆盖用例编号（对账后用例序号，1 起）
    suggestion: str = ""  # 非空 ⇒ 未覆盖登记的「具体设计建议」（从条款要素推导）

    @property
    def status(self) -> str:
        """核对表三态（迭代 9 / UP-1）。"""
        if self.suggestion:
            return "⚠ 未覆盖（已列清单）"
        return "✓ 已补齐（具体化补齐）" if self.filled else "✓ 质量覆盖"


@dataclass
class FRCoverageReport:
    """FR 对账报告（确定性输出，不含时间戳）。"""

    source: str
    items: list[FRCoverageItem] = field(default_factory=list)
    case_count: int = 0  # 对账 + 具体化补齐后的用例总数
    added_cases: int = 0  # 具体化补齐新增的用例数
    auto_numbered: bool = False  # True=原文档无编号，需求清单为自动编号兜底

    @property
    def total(self) -> int:
        """FR 条目总数。"""
        return len(self.items)

    @property
    def filled_count(self) -> int:
        """被具体化补齐的 FR 条数。"""
        return sum(1 for item in self.items if item.filled)

    @property
    def uncovered_items(self) -> list[FRCoverageItem]:
        """未覆盖清单（已登记具体设计建议的条目）。"""
        return [item for item in self.items if item.suggestion]

    @property
    def covered_count(self) -> int:
        """有覆盖用例（含具体化补齐）的 FR 条数。"""
        return self.total - len(self.uncovered_items)

    def summary_line(self) -> str:
        """覆盖率总结句（迭代 9：覆盖率如实计算，允许 <100%，禁止水分）。"""
        pct = round(self.covered_count * 100 / self.total) if self.total else 100
        parts = [f"FR 覆盖率 {pct}%（{self.covered_count}/{self.total}）"]
        if self.filled_count:
            parts.append(
                f"其中具体化补齐 {self.filled_count} 条 FR（新增 {self.added_cases} 条用例）"
            )
        if self.uncovered_items:
            parts.append(
                f"未覆盖 {len(self.uncovered_items)} 条已列入未覆盖清单（禁用模板填塞，覆盖率如实计算）"
            )
        if not self.filled_count and not self.uncovered_items:
            parts.append("全部 FR 由规则生成用例覆盖，无需补齐")
        return "，".join(parts)


# 断言片段切分（「」引文内部不切分）：摘要与要素提取共用
_SUMMARY_SPLIT_RE = re.compile(r"[，,。；;、：:！!？?（(【[]")
# 条款内「」提示文案 / 界面元素
_PROMPT_RE = re.compile(r"「([^」]+)」")
# 触发类行为词（有触发方向可验证 → 可产阈值两侧对照用例）
_TRIGGER_RE = re.compile(
    r"触发|锁定|拒绝|提示|失效|过期|不可|禁止|不得|错误|取消|拦截|挤下线|不触发"
)


def _fr_summary(text: str, limit: int = 24) -> str:
    """条款摘要：取条款核心短语（首个句读段），在语义边界截断且不带省略号
    （迭代 9 / F3 修 bug：绝不从数字 / 词语中间断开）。"""
    text = text.strip()
    first = _SUMMARY_SPLIT_RE.split(text)[0].strip()
    core = first or text
    return quality.semantic_trim(core, limit)


def _fr_module_path(fr: FRItem, profile: DocumentProfile, child: Optional[str] = None) -> str:
    """补齐用例所属模块：取条款所在小节的真实「父模块/子模块」路径
    （迭代 9 / UP-1 修 bug：不得把文档名当模块）；子模块按专项方法论命名。"""
    if child is None:
        kind = mock_engine.classify_requirement(fr.text, fr.section)
        child = {"性能测试": "性能验证", "安全测试": "安全验证"}.get(kind, "功能验证")
    parts = [p for p in (fr.section_path or "").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    if len(parts) == 1:
        return f"{parts[0]}/{child}"
    return f"需求清单/{child}"


def _clause_elements(fr: FRItem) -> tuple[list, list[str]]:
    """提取条款的具体可验证要素（迭代 9 / UP-1 具体化规则）：
    （数值 / 阈值 / 时间点边界列表, 「」提示文案列表）。"""
    boundaries = prd_adapter._extract_boundaries(fr.text)
    prompts = _PROMPT_RE.findall(fr.text)
    return boundaries, prompts


def _case_covers_fr(case: TestCase, fr: FRItem) -> bool:
    """用例覆盖 FR 的判定：标题带 [FR-x] 追溯前缀，或正文引用了需求条目原文。

    迭代 9 / F2：纯质量属性条款（无任何可验证要素，如「系统应具备良好的
    用户体验」）补不出可验证用例，任何引用（含场景法流程步）都不算可验证
    覆盖——登记未覆盖清单，不以流程引用充数。
    """
    if mock_engine._is_unverifiable_clause(fr.text):
        return False
    if fr.tag in case.title:
        return True
    blob = f"{case.title}\n{case.precondition}\n{case.steps}\n{case.expected}"
    return fr.text in blob


def _fr_backfill_cases(fr: FRItem, module: str) -> list[TestCase]:
    """缺漏 FR 的具体化补齐（迭代 9 / UP-1，废除模板填塞）。

    仅当条款含具体可验证要素（数值 / 阈值 / 时间点 / 「」提示文案 / 判定标准）
    时才生成用例，预期必须含具体验证点（数值与两侧相邻值 / 提示文案逐字引用），
    每个断言片段均引用条款原文（不引入 PRD 外规则与后果）；
    触发类条款产「阈值边界 + 相邻反例」2 条，判定标准类条款产「目标规模下满足
    判定标准」1 条（NFR 性能 / 容量条款禁止发明超容行为）；
    条款没有任何可验证要素（如「支持功能点N」）→ 返回 []（调用方登记未覆盖清单），
    不再塞「系统接受有效输入并处理成功」类同义反复模板。
    """
    boundaries, prompts = _clause_elements(fr)
    if not boundaries and not prompts:
        return []
    method = mock_engine.classify_requirement(fr.text, fr.section) or "等价类"
    cite = f"条款「{fr.text}」"
    summary = _fr_summary(fr.text)
    trigger = bool(_TRIGGER_RE.search(fr.text))
    numeric = [b for b in boundaries if b.kind != "timepoint"]
    timepoints = [b for b in boundaries if b.kind == "timepoint"]
    cases: list[TestCase] = []

    def _prompt_asserts() -> list[str]:
        return [f"{cite}：提示「{p}」逐字一致" for p in prompts]

    def _make(title: str, asserts: list[str], category: str, priority: str = "P1") -> TestCase:
        return TestCase(
            title=title,
            precondition=f"已满足「{summary}」的基本使用条件，测试数据已准备就绪",
            steps=(
                f"1. 按{cite}准备测试数据\n"
                f"2. 按{cite}调整输入 / 时点并执行对应操作\n"
                f"3. 核对{cite}的处理结果"
            ),
            expected="；".join(asserts + _prompt_asserts()),
            priority=priority,
            category=category,
            method=method,
            module=module,
        )

    if trigger and numeric:
        # 触发类条款：每个数值阈值各产 1 条「阈值 + 两侧相邻值」边界用例（UP-3 不得只测第一个）
        if method == "等价类":
            method = "边界值"
        for b in numeric:
            v = mock_engine._boundary_display(b, 0)
            lo = mock_engine._boundary_display(b, -1)
            hi = mock_engine._boundary_display(b, 1)
            cases.append(
                _make(
                    f"{fr.tag} 【{method}】{summary} - 阈值 {v} 边界验证（具体化补齐）",
                    [
                        f"{cite}：输入 {v}（恰好达到阈值）时按{cite}触发处理",
                        f"{cite}：输入 {lo}（少 1）未达阈值不触发",
                        f"{cite}：输入 {hi}（多 1）触发阈值并按{cite}处理",
                    ],
                    "边界",
                )
            )
    elif timepoints:
        # 时间点条款（迭代 9 / UP-3）：每个切换时刻各产 1 条「时刻前 / 整 / 后」对照用例
        if method == "等价类":
            method = "边界值"
        for b in timepoints:
            v = mock_engine._boundary_display(b, 0)
            lo = mock_engine._boundary_display(b, -1)
            hi = mock_engine._boundary_display(b, 1)
            cases.append(
                _make(
                    f"{fr.tag} 【{method}】{summary} - 切换时刻 {v} 前后对照（具体化补齐）",
                    [
                        f"{cite}：{lo}（切换时刻前）切换未生效",
                        f"{cite}：{v}（切换时刻整）切换生效",
                        f"{cite}：{hi}（切换时刻后）保持切换生效后的状态",
                    ],
                    "边界",
                )
            )
    else:
        # 判定标准类 / 提示文案类：每个数值判定标准各产 1 条「判定标准达成」用例
        # （NFR 性能 / 容量条款只验目标规模，不作超容行为断言）；无数值时产 1 条提示核对用例
        targets: list = numeric or [None]
        for b in targets:
            asserts = []
            title_suffix = "判定标准验证（具体化补齐）"
            if b is not None:
                v = mock_engine._boundary_display(b, 0)
                lo = mock_engine._boundary_display(b, -1)
                hi = mock_engine._boundary_display(b, 1)
                title_suffix = f"判定标准 {v} 验证（具体化补齐）"
                asserts.append(f"{cite}：在 {v}（判定标准）下满足{cite}的判定标准")
                asserts.append(f"{cite}：{lo}（少 1）与 {hi}（多 1）为对照点，按{cite}的判定标准记录")
            asserts.append(f"{cite}：结果与{cite}的判定标准逐项一致")
            cases.append(_make(f"{fr.tag} 【{method}】{summary} - {title_suffix}", asserts, "功能"))
    return cases


def build_design_suggestion(fr: FRItem) -> str:
    """未覆盖清单的具体设计建议（迭代 9 / UP-1）：从条款要素推导，禁止空话。

    阈值类给「边界两侧相邻值」建议，时间点类给「时刻前 / 整 / 后」建议，
    提示文案类给「逐字一致」建议；条款无任何可验证要素时明确指出
    缺失的验收要素（状态码 / 字段 / 数值 / 界面元素），不写「建议进一步测试」类空话。
    """
    summary = _fr_summary(fr.text)
    boundaries, prompts = _clause_elements(fr)
    parts: list[str] = []
    for b in boundaries:
        v = mock_engine._boundary_display(b, 0)
        lo = mock_engine._boundary_display(b, -1)
        hi = mock_engine._boundary_display(b, 1)
        if b.kind == "timepoint":
            parts.append(
                f"建议验证：{b.text} 切换时刻前（{lo}）/时刻整（{v}）/时刻后（{hi}）的行为对照"
            )
        else:
            parts.append(
                f"建议验证：{summary} 的阈值 {v} 边界（{lo} 不触发 / {v} 触发 / {hi} 触发）"
            )
    for p in prompts:
        parts.append(f"建议验证：条件满足时提示「{p}」逐字一致")
    if parts:
        return "；".join(parts)
    return (
        f"建议补充验收标准：条款「{summary}」未给出可验证判定标准"
        "（无状态码 / 字段 / 数值 / 界面元素），请补充量化验收标准后重新生成用例"
    )


def reconcile_fr_coverage(
    profile: DocumentProfile, cases: list[TestCase]
) -> tuple[list[TestCase], Optional[FRCoverageReport]]:
    """生成后 FR 逐条对账：覆盖核对 + 追溯前缀 + 具体化补齐 + 未覆盖登记，返回（用例集, 对账报告）。

    - 已覆盖的 FR 给首条覆盖用例补 [FR-x.y] 追溯前缀，登记覆盖用例编号（1 起）；
    - 未命中的 FR 仅当能生成「引用条款具体要素 + 可验证行为」的用例时才具体化补齐
      （标记「已补齐（具体化补齐）」）；补不出 → 登记「未覆盖清单」并给出从条款
      要素推导的具体设计建议（迭代 9 / UP-1：废除模板填塞，禁止凑数覆盖）；
    - 硬门禁「零遗漏登记」（迭代 9 / UP-1）：任何 FR 既无覆盖用例又不在未覆盖
      清单 → 抛中文异常使生成失败（exit 1）；覆盖率如实计算（允许 <100%）；
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
                FRCoverageItem(
                    fr_id=fr.id,
                    summary=_fr_summary(fr.text),
                    count=len(covering),
                    filled=False,
                    case_numbers=[i + 1 for i in covering],
                )
            )
            continue
        backfill = _fr_backfill_cases(fr, _fr_module_path(fr, profile))
        if backfill:
            start = len(work)
            work.extend(backfill)
            added += len(backfill)
            items.append(
                FRCoverageItem(
                    fr_id=fr.id,
                    summary=_fr_summary(fr.text),
                    count=len(backfill),
                    filled=True,
                    case_numbers=[start + 1 + j for j in range(len(backfill))],
                )
            )
        else:
            # 补不出具体用例 → 登记未覆盖清单（含具体设计建议），不再模板填塞
            items.append(
                FRCoverageItem(
                    fr_id=fr.id,
                    summary=_fr_summary(fr.text),
                    count=0,
                    filled=False,
                    case_numbers=[],
                    suggestion=build_design_suggestion(fr),
                )
            )

    # 硬门禁（迭代 9 / UP-1）：零遗漏登记——每条 FR 要么有覆盖用例，
    # 要么已在未覆盖清单登记具体设计建议，两者皆无 → 生成失败（exit 1）
    missing = [item.fr_id for item in items if item.count == 0 and not item.suggestion]
    if missing:
        raise ValueError(
            f"零遗漏登记未满足：{len(missing)} 条需求既无覆盖用例又未登记未覆盖清单"
            f"（{'、'.join(missing)}，共 {len(profile.frs)} 条需求），生成失败"
        )

    return work, FRCoverageReport(
        source=profile.source,
        items=items,
        case_count=len(work),
        added_cases=added,
        auto_numbered=bool(profile.frs) and all(fr.auto for fr in profile.frs),
    )


def format_fr_coverage_report(report: FRCoverageReport) -> str:
    """把 FR 对账结果渲染为全中文 Markdown 覆盖率核对表（确定性，不含时间戳）。

    核对表为「PRD 条款 → 用例编号」映射（迭代 8 / UP-1，迭代 9 三态）：
    列 = 编号（逐字引用 PRD 原文编号）| 原文摘要 | 覆盖用例编号列表 | 状态，
    状态三态：✓ 质量覆盖 / ✓ 已补齐（具体化补齐）/ ⚠ 未覆盖（已列清单）；
    未覆盖清单专节逐条给出「具体设计建议」（从条款要素推导，禁止空话）；
    编号来源说明区分「原文编号」与「自动编号兜底」两种情况（迭代 7 / UP-1）。
    """
    lines = [
        f"# FR 需求对账报告：{Path(report.source).name}",
        "",
        f"- 输入文档：{report.source}",
        f"- 需求条目：{report.total} 条；对账后用例：{report.case_count} 条",
        (
            "- 编号说明：本文档未识别到原文编号，需求清单采用自动编号兜底"
            "（FR-auto-1…，用例标题追溯前缀与自动编号一致）"
            if report.auto_numbered
            else "- 编号说明：编号逐字引用 PRD 原文编号（FR / NFR 分层），"
            "用例标题追溯前缀与原文编号逐字一致"
        ),
        "- 覆盖用例编号：对账后用例序号（1 起），与导出用例集顺序一致",
        "- 状态口径：✓ 质量覆盖 / ✓ 已补齐（具体化补齐）/ ⚠ 未覆盖（已列清单）；"
        "覆盖率如实计算，禁用模板填塞（迭代 9 / UP-1）",
        "",
        "## 覆盖率核对表（PRD 条款 → 用例编号）",
        "",
        "| 编号 | 原文摘要 | 覆盖用例编号 | 状态 |",
        "| --- | --- | --- | --- |",
    ]
    for item in report.items:
        numbers = "、".join(f"#{n}" for n in item.case_numbers) or "—"
        lines.append(f"| {item.fr_id} | {item.summary} | {numbers} | {item.status} |")
    lines.append("")
    lines.append(report.summary_line())
    if report.uncovered_items:
        lines.extend(
            [
                "",
                "## 未覆盖清单（含具体设计建议）",
                "",
                "以下需求补不出「引用条款具体要素 + 可验证行为」的具体用例，"
                "已如实登记未覆盖（不以模板条目凑数）：",
                "",
            ]
        )
        for item in report.uncovered_items:
            lines.append(f"- **{item.fr_id}**（原文摘要：{item.summary}）：{item.suggestion}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 增量生成（迭代 10 / UP-2）：--only 指定 FR 重新生成 + 与已有用例集合并
# ---------------------------------------------------------------------------


def resolve_only_targets(profile: DocumentProfile, only: Sequence[str]) -> list[FRItem]:
    """把 --only 需求编号（FR-3 / FR-3.1 / NFR-1.2，多值逗号分隔）解析为 FR 条目。

    编号与 PRD 原文编号逐字比对；任一编号不存在即抛中文错误并列出可选编号。
    """
    wanted: list[str] = []
    for item in only:
        wanted.extend(s.strip() for s in str(item).split(",") if s.strip())
    by_id = {fr.id: fr for fr in profile.frs}
    unknown = [w for w in wanted if w not in by_id]
    if unknown:
        raise ValueError(
            f"增量生成指定的需求编号不存在：{'、'.join(unknown)}"
            f"（可选编号：{'、'.join(by_id) or '无'}，支持 FR-3 / FR-3.1 / NFR-1.2 多值逗号分隔）"
        )
    return [by_id[w] for w in dict.fromkeys(wanted)]


def merge_incremental_cases(
    cached: Sequence[TestCase], generated: Sequence[TestCase], targets: Sequence[FRItem]
) -> tuple[list[TestCase], list[TestCase]]:
    """增量合并（迭代 10 / UP-2）：已有用例集中剔除目标 FR 归属用例，
    替换为本次重新生成的目标 FR 归属用例；未归属任何目标 FR 的用例原样保留。

    返回（合并后用例集, 本次重新生成的目标 FR 用例）。归属判定与 FR 对账同口径
    （_case_covers_fr：[FR-x] 追溯前缀或引用条款原文）。
    """

    def belongs(case: TestCase) -> bool:
        return any(_case_covers_fr(case, fr) for fr in targets)

    kept = [c for c in cached if not belongs(c)]
    fresh = [c for c in generated if belongs(c)]
    return kept + fresh, fresh


def generate_from_file(
    path: Union[str, Path],
    mode: Optional[LLMMode] = None,
    only: Optional[Sequence[str]] = None,
) -> GenerationResult:
    """端到端生成：文件 → 用例集（全部经 pydantic 校验）。

    迭代 10 / UP-2：
    - 解析缓存：输入内容未变时读取 output/.cache/<内容 hash>.json 跳过重新解析
      （日志明示「读取解析缓存」），输入变更自动失效；
    - 增量生成（only 非空）：只重新生成指定 FR 归属的用例，与已有用例集
      （上次生成结果缓存）合并后整体返回；等价性硬要求——同一输入下逐条
      --only 触发并合并 == 整份生成的用例集（内容一致、顺序允许不同）；
    - 未指定 only 时行为与迭代 9 完全一致（含 FR 对账报告落盘）。
    """
    p = Path(path)
    digest = input_content_hash(p)
    logger.info("正在解析输入文档：%s", p)
    profile = load_parse_cache(digest)
    if profile is None:
        profile = parse_document(p)
        save_parse_cache(digest, profile)
    logger.info(
        "解析完成：模块「%s」，功能点 / 接口 %d 个，数字边界 %d 个，主流程步骤 %d 步",
        profile.module, len(profile.features), len(profile.boundaries), len(profile.flows),
    )

    targets: list[FRItem] = []
    if only:
        targets = resolve_only_targets(profile, only)

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

    # FR 需求对账（迭代 6 / UP-1）：逐条覆盖核对 + 具体化补齐 + 报告打印落盘
    cases, fr_report = reconcile_fr_coverage(profile, cases)
    fr_summary = ""
    if fr_report is not None and not targets:
        text = format_fr_coverage_report(fr_report)
        for line in text.splitlines():
            logger.info(line)
        out_dir = Path(FR_REPORT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / f"{p.stem}_fr_coverage.md"
        report_path.write_text(text, encoding="utf-8")
        logger.info("FR 对账报告已落盘：%s", report_path)
        fr_summary = fr_report.summary_line()

    # 依据 / 假设标注（迭代 9 / UP-2）：经幻觉溯源判定为 [假设] 的衍生 / 假设类
    # 用例在关键词列标注「,假设」（如「场景法,假设」）；mock 规则直出用例全部
    # 有据（回归断言保证），不会命中；LLM 模式产出的衍生用例借此显式标注
    cases = _tag_assumption_cases(cases, p.read_text(encoding="utf-8"), profile)

    if not targets:
        # 完整生成：用例集缓存即「上次生成结果缓存」（增量生成的合并基线）
        save_case_cache(digest, cases)
        return GenerationResult(
            source=str(p),
            cases=cases,
            mode=gateway.mode,
            generated_at=datetime.now(),
            fr_coverage_summary=fr_summary,
        )

    # 增量生成（迭代 10 / UP-2）：只重新生成指定 FR 归属用例，与已有用例集合并
    cached = load_case_cache(digest)
    if not cached:
        logger.warning(
            "未找到上次生成结果缓存（%s），本次仅导出指定 FR 的用例；"
            "先执行一次完整生成即可合并整份用例集",
            _cache_file(digest, CASES_CACHE_SUFFIX),
        )
    merged, fresh = merge_incremental_cases(cached or [], cases, targets)
    if not merged:
        raise ValueError(
            f"增量生成结果为空：{'、'.join(fr.id for fr in targets)} 未产出任何用例，"
            "无可导出内容（请确认指定的需求编号含可验证要素）"
        )
    save_case_cache(digest, merged)
    fr_summary = (
        f"增量生成：已重新生成 {'、'.join(fr.id for fr in targets)} 归属用例 {len(fresh)} 条，"
        f"与已有用例集合并后共 {len(merged)} 条"
    )
    logger.info(fr_summary)
    return GenerationResult(
        source=str(p),
        cases=merged,
        mode=gateway.mode,
        generated_at=datetime.now(),
        fr_coverage_summary=fr_summary,
    )


def _tag_assumption_cases(
    cases: list[TestCase], source_text: str, profile: DocumentProfile
) -> list[TestCase]:
    """幻觉溯源判定 [假设] 的用例打「,假设」关键词标注（迭代 9 / UP-2）。

    逐条执行 check_grounding：用例断言超出 PRD 条款依据（含 PRD 无据的契约
    细节与虚构规则）→ 来源标注 [假设] → 用例 assumption=True（导出时关键词列
    显示「场景法,假设」，pytest / Markdown 旁注「（设计假设）」）；
    [PRD §编号] 依据用例不受影响，[测量值预言] 按禁令拦截不降级为假设。
    """
    grounding = quality.check_grounding(
        cases, source_text, profile.frs, source_name=profile.source
    )
    assumed = {s.index for s in grounding.sources if s.label == quality.LABEL_ASSUMPTION}
    if not assumed:
        return list(cases)
    return [
        case.model_copy(update={"assumption": True}) if idx in assumed else case
        for idx, case in enumerate(cases, 1)
    ]
