"""确定性规则式 mock 生成器（无 key / 无 Ollama 时的最终降级方案）。

设计原则：同一输入必须输出完全相同的结果（不含随机数、不含时间戳）。
生成规则（与 core/methodology.py 的方法论模板保持一致）：
1. 数字 / 比较词边界（≥/≤/不超过/至少/超过 等）→ 边界值用例（取边界值与边界 ±1）
2. 功能点 / 动宾短语 → 等价类用例（有效类 + 无效类）
3. 主流程操作序列 → 场景法用例（基本流 / 备选流 / 异常流）
4. 优先级规则：主流程 P0、有效等价类与边界 P1、异常 / 无效类 P2
5. 多条件组合 → 判定表用例；最终保证每文档 ≥8 条用例，
   method 覆盖 ≥3 种、category 覆盖 ≥3 种
6. 接口端点补齐（仅 OpenAPI 文档，迭代 4 新增）：主流程未覆盖的资源
   （按接口 path 首段分组，如 /user、/store/order）各补 1 条「场景法·接口」
   专项用例；参数边界缺口最大的资源合并补 1 条边界值专项用例
   （引用具体边界值与两侧相邻值），补齐 /user 类端点覆盖而不灌水用例规模
7. 安全 / 性能类需求识别（迭代 6 方法论标注校准）：需求条目含安全类词
   （安全、权限、越权、篡改、攻击、注入、脱敏、锁定、验证码等）→ 对应用例
   method=安全测试；含性能类词（性能、并发、响应时间、压力、QPS、吞吐、
   耗时等）→ method=性能测试，不再硬套边界值（识别规则见 classify_requirement）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .schema import CATEGORIES, METHODS, Boundary, DocumentProfile, Feature

# 用例数量与覆盖度下限（UP-3 验收标准）
MIN_CASES = 8
MIN_METHOD_COVERAGE = 3
MIN_CATEGORY_COVERAGE = 3

# 单文档生成规模上限（少而精：避免机械用例灌水）
MAX_BOUNDARIES = 5
MAX_FEATURES = 6

# 规则 6（接口端点补齐）规模上限：每个资源最多 1 条场景法专项，
# 参数边界专项每文档合并 1 条（引用的边界数有上限），防止用例灌水
MAX_ENDPOINT_SCENARIOS = 3
MAX_ENDPOINT_BOUNDARIES = 2

# 接口操作名解析：如「POST /user（创建用户）」→（POST, /user）
_API_OP_RE = re.compile(r"^([A-Z]+)\s+(/\S*?)(?:（|$)")

# 补齐用例只在四种经典方法论内轮转（安全 / 性能测试是内容驱动的专项标注，
# 不作为覆盖度补齐目标，保证补齐行为与既有回归一致）
_PAD_METHODS = ("等价类", "边界值", "场景法", "判定表")

# ---------------------------------------------------------------------------
# 安全 / 性能类需求识别（迭代 6 方法论标注校准）
# ---------------------------------------------------------------------------

# 安全类强语义词：任意语境命中即标注「安全测试」
_SECURITY_STRONG_RE = re.compile(
    r"越权|篡改|攻击|注入|脱敏|加密|敏感信息|暴力破解|防爆破|XSS|CSRF"
)
# 安全类泛化词：单独出现时可能只是功能描述（如「支持图形验证码登录」），
# 必须结合约束语境才判定，避免普通功能需求被误标为安全测试
_SECURITY_SOFT_RE = re.compile(r"安全|权限|锁定|验证码")
# 性能类词：语义明确，命中即标注「性能测试」（原有 3 样例均不含这些词）
_PERF_RE = re.compile(r"性能|并发|响应时间|压力|QPS|qps|吞吐|耗时")
# 约束语境一：条目自身是约束性表述（防 / 禁止 / 不得 等）
_CONSTRAINT_RE = re.compile(r"防止|防范|禁止|不得|不允许|必须|要求|拦截|防护|防(?!御)")
# 约束语境二：条目位于安全 / 权限 / 非功能类小节
_SECTION_CONTEXT_RE = re.compile(r"安全|权限|非功能|约束|限制")


def classify_requirement(text: str, section: str = "") -> Optional[str]:
    """按需求条目内容识别安全 / 性能类需求，返回专项方法论标注，无关返回 None。

    设计取舍（可面试讲述）：纯关键词匹配会把「支持图形验证码登录」这类功能需求
    误标为安全测试，导致原有样例方法论分布漂移。因此安全类采用两档判定——
    强语义词（注入 / 脱敏 / 越权 / 篡改 / 攻击等）直接标注「安全测试」；
    泛化词（安全 / 权限 / 锁定 / 验证码）需条目处于约束语境（防 / 禁止 等
    约束动词，或安全 / 非功能类小节）才标注。性能类词（性能 / 并发 /
    响应时间 / 压力 / QPS / 吞吐 / 耗时）语义明确，直接标注「性能测试」。
    """
    if _SECURITY_STRONG_RE.search(text):
        return "安全测试"
    if _PERF_RE.search(text):
        return "性能测试"
    if _SECURITY_SOFT_RE.search(text) or _SECURITY_SOFT_RE.search(section):
        if _CONSTRAINT_RE.search(text) or _SECTION_CONTEXT_RE.search(section):
            return "安全测试"
    return None


def _fmt_num(value: float) -> str:
    """把数值格式化为整洁的中文文本（整数不带小数点）。"""
    return f"{value:g}"


def _boundary_cases(b: Boundary, module: str) -> list[dict]:
    """规则 1：数字边界 → 边界值用例（边界值本身 P1、边界 ±1 P2）。

    步骤与预期结果只允许引用 PRD 条款要素（b.text 原文）与标准测试推导值
    （边界 ±1 相邻值）并写明方向（接受 / 拒绝 / 触发），每个断言片段均带
    条款引用，不引入 PRD 之外的规则与后果（迭代 7 幻觉溯源：清除编造规则模板）。
    安全 / 性能类需求的边界不再硬套「边界值」标注，改标专项方法论（迭代 6）。
    """
    # 专项方法论标注：安全 / 性能类需求 → 安全测试 / 性能测试，其余保持边界值
    method = classify_requirement(b.text, b.section) or "边界值"
    value = _fmt_num(b.value)
    unit = b.unit
    lo = _fmt_num(b.value - 1)
    hi = _fmt_num(b.value + 1)
    cite = f"条款「{b.text}」"
    if b.kind == "range_min":
        inside_desc, outside_desc = f"最小值 {value}{unit}", f"低于最小值 {lo}{unit}"
        inside_expected = (
            f"{cite}：输入 {value}{unit}（恰好达到最小值）时被接受，输入 {hi}{unit}（多 1）被接受；"
            f"{cite}：输入 {lo}{unit}（少 1）被拒绝"
        )
        outside_expected = (
            f"{cite}：输入 {lo}{unit}（低于最小值 {value}{unit}）时被拒绝；"
            f"{cite}：输入 {value}{unit}（最小值）被接受，输入 {hi}{unit}（多 1）被接受"
        )
    elif b.kind == "range_max":
        inside_desc, outside_desc = f"最大值 {value}{unit}", f"超过最大值 {hi}{unit}"
        inside_expected = (
            f"{cite}：输入 {value}{unit}（恰好达到最大值）时被接受，输入 {lo}{unit}（少 1）被接受；"
            f"{cite}：输入 {hi}{unit}（多 1）被拒绝"
        )
        outside_expected = (
            f"{cite}：输入 {hi}{unit}（超过最大值 {value}{unit}）时被拒绝；"
            f"{cite}：输入 {value}{unit}（最大值）被接受，输入 {lo}{unit}（少 1）被接受"
        )
    else:
        inside_desc = f"恰好达到阈值 {value}{unit}"
        outside_desc = f"阈值相邻值 {lo}{unit} / {hi}{unit}"
        inside_expected = (
            f"{cite}：输入 {value}{unit}（恰好达到阈值）时被接受；"
            f"{cite}：输入 {hi}{unit}（多 1）触发阈值并被拒绝；"
            f"{cite}：输入 {lo}{unit}（少 1）未达阈值不触发"
        )
        outside_expected = (
            f"{cite}：阈值 {value}{unit}，输入 {lo}{unit}（少 1）未达阈值不触发，"
            f"输入 {hi}{unit}（多 1）触发阈值并被拒绝"
        )

    return [
        {
            "title": f"【{method}】{b.text} - 取边界值{inside_desc}",
            "precondition": "系统处于功能可用状态，测试数据已准备就绪",
            "steps": (
                f"1. 按{cite}准备测试数据\n"
                f"2. 将输入调整为{inside_desc}（{cite}）\n"
                f"3. 提交操作并观察{cite}的处理结果"
            ),
            "expected": inside_expected,
            "priority": "P1",
            "category": "边界",
            "method": method,
            "module": f"{module}/边界验证",
        },
        {
            "title": f"【{method}】{b.text} - 取越界值{outside_desc}",
            "precondition": "系统处于功能可用状态，测试数据已准备就绪",
            "steps": (
                f"1. 按{cite}准备测试数据\n"
                f"2. 将输入调整为{outside_desc}（{cite}）\n"
                f"3. 提交操作并观察{cite}的处理结果"
            ),
            "expected": outside_expected,
            "priority": "P2",
            "category": "边界",
            "method": method,
            "module": f"{module}/边界验证",
        },
    ]


def _equivalence_cases(f: Feature, module: str) -> list[dict]:
    """规则 2：功能点 / 接口 → 等价类用例（有效类 P1 + 无效类 P2）。

    步骤与预期结果只引用功能点 / 接口原文（f.name）等 PRD 条款要素，
    每个断言片段均带条款引用，不写 PRD 之外的规则与后果（迭代 7 幻觉溯源）。
    安全 / 性能类需求改标专项方法论（迭代 6），标题标记与 method 保持一致。
    """
    valid_category = "接口" if f.kind == "api" else "功能"
    method = classify_requirement(f.name, f.section) or "等价类"
    if method == "等价类":
        valid_title = f"【等价类】{f.name} - 有效等价类验证"
        invalid_title = f"【等价类】{f.name} - 无效等价类验证"
        valid_expected = (
            f"条款「{f.name}」：系统接受有效等价类输入并成功执行「{f.name}」；"
            f"条款「{f.name}」：返回结果与「{f.name}」的需求定义一致"
        )
        invalid_expected = (
            f"条款「{f.name}」：系统拒绝「{f.name}」的无效等价类输入；"
            f"条款「{f.name}」：「{f.name}」不被执行；"
            f"条款「{f.name}」：返回结果与「{f.name}」的拒绝定义一致"
        )
    else:
        valid_title = f"【{method}】{f.name} - 有效输入验证"
        invalid_title = f"【{method}】{f.name} - 异常输入验证"
        valid_expected = (
            f"条款「{f.name}」：系统按「{f.name}」的需求规则处理有效输入并处理成功；"
            f"条款「{f.name}」：返回结果与「{f.name}」的需求定义一致"
        )
        invalid_expected = (
            f"条款「{f.name}」：系统拦截「{f.name}」的无效输入；"
            f"条款「{f.name}」：「{f.name}」不被执行；"
            f"条款「{f.name}」：返回结果与「{f.name}」的拦截定义一致"
        )
    return [
        {
            "title": valid_title,
            "precondition": f"已满足「{f.name}」的基本使用条件",
            "steps": (
                f"1. 按条款「{f.name}」准备合法有效的输入数据\n"
                f"2. 执行「{f.name}」操作\n"
                f"3. 观察结果并核对条款「{f.name}」"
            ),
            "expected": valid_expected,
            "priority": "P1",
            "category": valid_category,
            "method": method,
            "module": f"{module}/功能验证",
        },
        {
            "title": invalid_title,
            "precondition": f"已满足「{f.name}」的基本使用条件",
            "steps": (
                f"1. 按条款「{f.name}」准备非法 / 缺失 / 超范围的无效输入数据\n"
                f"2. 执行「{f.name}」操作\n"
                f"3. 观察校验结果并核对条款「{f.name}」"
            ),
            "expected": invalid_expected,
            "priority": "P2",
            "category": "异常",
            "method": method,
            "module": f"{module}/异常验证",
        },
    ]


def _pick_scenario_boundary(profile: DocumentProfile) -> Optional[Boundary]:
    """为场景法异常流挑选可引用的边界要素：优先阈值类（如「连续错误 5 次」）。"""
    for b in profile.boundaries:
        if b.kind == "threshold":
            return b
    return profile.boundaries[0] if profile.boundaries else None


def _scenario_cases(profile: DocumentProfile) -> list[dict]:
    """规则 3：主流程操作序列 → 场景法用例（基本流 / 备选流 / 异常流）。

    步骤与预期结果引用主流程原文步骤与文档中的具体边界要素（条款引用），
    每个断言片段均带条款 / 流程原文引用，不写 PRD 之外的规则与后果
    （迭代 7 幻觉溯源：清除历史模板中编造的回滚 / 落库类后果表述）。
    """
    module = profile.module
    flows = profile.flows or [f"完成「{module}」的核心操作"]
    flow_steps = "\n".join(f"{i}. {step}" for i, step in enumerate(flows, 1))
    flow_line = " → ".join(flows[:3])
    flow_quotes = "、".join(f"「{s}」" for s in flows[:3])
    alt_anchor = flows[1] if len(flows) > 1 else flows[0]
    boundary = _pick_scenario_boundary(profile)
    boundary_ref = ""
    if boundary is not None:
        boundary_ref = (
            f"；条款「{boundary.text}」：触发「{boundary.text}」"
            f"（{_fmt_num(boundary.value)}{boundary.unit}）时按条款「{boundary.text}」处理"
        )
    return [
        {
            "title": f"【场景法】基本流：{flow_line}",
            "precondition": "初始状态正常，测试账号 / 数据已准备就绪",
            "steps": flow_steps,
            "expected": (
                f"条款「{flows[0]}」：依次完成{flow_quotes}后业务目标达成"
                f"（主流程：{flow_line}）"
            ),
            "priority": "P0",
            "category": "功能",
            "method": "场景法",
            "module": f"{module}/场景验证",
        },
        {
            "title": "【场景法】备选流：可选步骤跳过或中断后恢复",
            "precondition": "初始状态正常，测试账号 / 数据已准备就绪",
            "steps": (
                f"1. 按主流程执行：{flow_line}\n"
                f"2. 在「{alt_anchor}」环节跳过可选项或临时中断操作\n"
                f"3. 恢复「{alt_anchor}」并继续执行剩余步骤"
            ),
            "expected": (
                f"条款「{alt_anchor}」：在「{alt_anchor}」环节跳过可选项后可继续完成剩余步骤；"
                f"条款「{alt_anchor}」：在「{alt_anchor}」环节临时中断后恢复并继续完成剩余步骤"
            ),
            "priority": "P1",
            "category": "功能",
            "method": "场景法",
            "module": f"{module}/场景验证",
        },
        {
            "title": "【场景法】异常流：中途失败后的回滚与提示",
            "precondition": "初始状态正常，测试账号 / 数据已准备就绪",
            "steps": (
                f"1. 按主流程执行：{flow_line}\n"
                f"2. 在「{flows[0]}」环节注入异常并观察系统响应\n"
                f"3. 观察「{flows[0]}」的错误处理结果"
            ),
            "expected": (
                f"条款「{flows[0]}」：在「{flows[0]}」环节注入异常时系统拒绝该次操作"
                f"{boundary_ref}"
            ),
            "priority": "P2",
            "category": "异常",
            "method": "场景法",
            "module": f"{module}/异常验证",
        },
    ]


def _decision_table_cases(profile: DocumentProfile) -> list[dict]:
    """规则 5：多条件组合 → 判定表用例（条件全成立 / 部分不成立）。

    条件桩与预期结果只引用文档要素原文（功能点 / 边界 / 主流程），
    每个断言片段均带条款引用，不写 PRD 之外的规则与后果（迭代 7 幻觉溯源）。
    """
    module = profile.module
    conditions: list[str] = []
    if profile.features:
        conditions.append(f"满足「{profile.features[0].name}」的输入条件")
    if profile.boundaries:
        b = profile.boundaries[0]
        conditions.append(f"满足「{b.text}」的数值条件")
    if profile.flows:
        conditions.append(f"主流程「{profile.flows[0]}」可正常执行")
    if not conditions:
        conditions.append("多条件组合场景的全部条件")

    category = "接口" if any(f.kind == "api" for f in profile.features) else "功能"
    cond_join = "；".join(conditions)
    return [
        {
            "title": "【判定表】多条件组合 - 全部条件成立",
            "precondition": "测试环境与数据满足全部条件的准备要求",
            "steps": (
                f"1. 按判定表逐项确认条件：{cond_join}\n"
                f"2. 条件全部成立时执行目标操作：{cond_join}\n"
                f"3. 核对系统动作与判定表定义一致：{cond_join}"
            ),
            "expected": f"全部条件成立时系统执行目标操作并与判定表定义一致：{cond_join}",
            "priority": "P1",
            "category": category,
            "method": "判定表",
            "module": f"{module}/组合验证",
        },
        {
            "title": "【判定表】多条件组合 - 部分条件不成立",
            "precondition": "测试环境与数据满足部分条件的准备要求",
            "steps": (
                f"1. 构造仅部分条件成立的输入组合：{cond_join}\n"
                f"2. 执行目标操作：{cond_join}\n"
                f"3. 核对系统动作与判定表定义一致：{cond_join}"
            ),
            "expected": (
                f"仅部分条件成立时系统拒绝该操作并走判定表定义的备选动作：{cond_join}"
            ),
            "priority": "P2",
            "category": "异常",
            "method": "判定表",
            "module": f"{module}/组合验证",
        },
    ]


@dataclass
class _ResourceGroup:
    """接口资源分组：path 首段为键（如 user / store），合并路径词元与所属接口。"""

    key: str
    tokens: set[str] = field(default_factory=set)
    features: list[Feature] = field(default_factory=list)


def _api_resource(feature_name: str) -> Optional[tuple[str, set[str]]]:
    """从接口名解析资源键与路径词元（如「POST /user（创建用户）」→（user, {user}））。

    非「METHOD /path」格式的功能点（如 PRD 动宾短语）返回 None。
    """
    m = _API_OP_RE.match(feature_name.strip())
    if not m:
        return None
    segs = [s.strip("{}").lower() for s in m.group(2).split("/") if s.strip("{}")]
    if not segs:
        return None
    return segs[0], set(segs)


def _resource_groups(profile: DocumentProfile) -> list[_ResourceGroup]:
    """按资源（path 首段）对 API 功能点分组，保持文档首次出现顺序。"""
    groups: list[_ResourceGroup] = []
    index: dict[str, int] = {}
    for f in profile.features:
        parsed = _api_resource(f.name)
        if parsed is None:
            continue
        key, tokens = parsed
        if key not in index:
            index[key] = len(groups)
            groups.append(_ResourceGroup(key=key))
        group = groups[index[key]]
        group.tokens.update(tokens)
        group.features.append(f)
    return groups


def _flow_covered_keys(profile: DocumentProfile, groups: list[_ResourceGroup]) -> set[str]:
    """主流程已覆盖的资源键：资源的任一接口名出现在主流程步骤文本中。"""
    flow_text = "\n".join(profile.flows)
    return {g.key for g in groups if any(f.name in flow_text for f in g.features)}


def _resource_boundaries(group: _ResourceGroup, boundaries: list[Boundary]) -> list[Boundary]:
    """匹配资源的参数边界：边界描述文本包含任一路径词元（如 user → schema User.xxx）。"""
    return [b for b in boundaries if any(t in b.text.lower() for t in group.tokens)]


def _boundary_direction_text(b: Boundary) -> str:
    """单个边界的数值化预期文本：边界值 + 两侧相邻值 + 方向（接受 / 拒绝 / 触发）。

    只引用边界原文（调用方以「{b.text}」条款引用包裹）与边界 ±1 推导值，
    不写 PRD 之外的规则与后果（迭代 7 幻觉溯源）。
    """
    value = _fmt_num(b.value)
    lo = _fmt_num(b.value - 1)
    hi = _fmt_num(b.value + 1)
    if b.kind == "range_min":
        return (
            f"输入 {value}{b.unit}（恰好达到最小值）被接受，"
            f"{lo}{b.unit}（少 1）被拒绝，{hi}{b.unit}（多 1）被接受"
        )
    if b.kind == "range_max":
        return (
            f"输入 {value}{b.unit}（恰好达到最大值）被接受，"
            f"{hi}{b.unit}（多 1）被拒绝，{lo}{b.unit}（少 1）被接受"
        )
    return (
        f"输入 {value}{b.unit}（恰好达到阈值）触发规则处理，"
        f"{lo}{b.unit}（少 1）未达阈值不触发，{hi}{b.unit}（多 1）触发阈值并被拒绝"
    )


def _endpoint_cases(profile: DocumentProfile) -> list[dict]:
    """规则 6：接口端点补齐（仅 OpenAPI 文档）——评测驱动改进（迭代 4）。

    评测发现 mock 生成集「基准要素重合度」缺口集中在主流程未覆盖的端点资源
    （如 /user）：既有规则只按文档顺序取前 N 个边界 / 功能点，靠后资源的
    场景类与参数边界类用例被截断。本规则：
    1. 按 path 首段分组资源，找出主流程（场景法基本流）未覆盖的资源；
    2. 每个缺口资源补 1 条「场景法 · 接口」专项（预期引用该资源接口名）；
    3. 参数边界缺口最大的资源合并补 1 条边界值专项（≤MAX_ENDPOINT_BOUNDARIES
       个边界，预期含边界值与两侧相邻值），少而精控制用例规模。
    """
    if profile.source_type != "openapi":
        return []
    groups = _resource_groups(profile)
    if not groups:
        return []
    covered = _flow_covered_keys(profile, groups)
    gap_groups = [g for g in groups if g.key not in covered]
    if not gap_groups:
        return []

    module = profile.module
    cases: list[dict] = []
    for group in gap_groups[:MAX_ENDPOINT_SCENARIOS]:
        names = "、".join(f"「{f.name}」" for f in group.features[:2])
        call_lines = "\n".join(
            f"{i}. 调用「{f.name}」并记录响应状态与响应体"
            for i, f in enumerate(group.features[:3], 2)
        )
        cases.append(
            {
                "title": f"【场景法】{group.key} 资源接口主流程专项：{group.features[0].name}",
                "precondition": f"接口服务可用，{group.key} 资源测试数据已准备就绪",
                "steps": (
                    f"1. 按「{group.features[0].name}」准备{group.key} 资源的合法测试数据\n"
                    f"{call_lines}\n"
                    f"3. 核对「{group.features[0].name}」的资源状态与响应数据"
                ),
                "expected": (
                    f"依次调用{names}完成{group.key} 资源接口主流程："
                    f"各接口响应状态与「{group.features[0].name}」的接口定义一致"
                ),
                "priority": "P1",
                "category": "接口",
                "method": "场景法",
                "module": f"{module}/场景验证",
            }
        )

    # 参数边界专项：投给缺口最大的资源（按未覆盖边界数降序、文档顺序取先）
    selected = set(id(b) for b in profile.boundaries[:MAX_BOUNDARIES])
    def _gap_size(group: _ResourceGroup) -> int:
        return sum(1 for b in _resource_boundaries(group, profile.boundaries) if id(b) not in selected)

    target = max(gap_groups, key=_gap_size)
    uncovered = [b for b in _resource_boundaries(target, profile.boundaries) if id(b) not in selected]
    if uncovered:
        merged = uncovered[:MAX_ENDPOINT_BOUNDARIES]
        parts = "；".join(f"「{b.text}」：{_boundary_direction_text(b)}" for b in merged)
        cases.append(
            {
                "title": (
                    f"【边界值】{target.key} 资源参数边界专项 - "
                    + "、".join(b.text for b in merged)
                ),
                "precondition": f"接口服务可用，{target.key} 资源接口可正常调用",
                "steps": (
                    f"1. 按「{merged[0].text}」准备{target.key} 资源接口的参数边界测试数据\n"
                    f"2. 按「{merged[0].text}」分别提交边界值与两侧相邻值参数\n"
                    f"3. 核对「{merged[0].text}」的接口响应与参数校验结果"
                ),
                "expected": parts,
                "priority": "P1",
                "category": "边界",
                "method": "边界值",
                "module": f"{module}/边界验证",
            }
        )
    return cases


def _concrete_anchor(profile: DocumentProfile) -> str:
    """从文档要点中取一个具体锚点（功能点 / 流程步骤 / 边界），供补齐用例引用（UP-3）。

    文档无任何要点时返回空字符串（补齐用例退回通用文案）。
    """
    if profile.features:
        return f"「{profile.features[0].name}」"
    if profile.flows:
        return f"「{profile.flows[0]}」"
    if profile.boundaries:
        b = profile.boundaries[0]
        return f"「{b.text}」（{_fmt_num(b.value)}{b.unit}）"
    return ""


def _pad_cases(raw_cases: list[dict], profile: DocumentProfile) -> list[dict]:
    """确定性补齐：保证 ≥8 条用例，且 method / category 覆盖达标。

    补齐落到「边界值」时，预期结果必须引用具体数值与两侧相邻值
    （UP-3：边界值用例禁止通用套话）：优先引用文档真实边界条款原文与
    边界 ±1 推导值（迭代 7 幻觉溯源：不编造文档外的数量规则），
    文档完全没有边界时才退回「1 件」锚点（0/1/2 三个确定值）；
    其余补齐用例的断言片段引用文档要点锚点，不写 PRD 之外的规则。
    """
    module = profile.module
    anchor = _concrete_anchor(profile)
    i = 0
    while True:
        used_methods = {c["method"] for c in raw_cases}
        used_categories = {c["category"] for c in raw_cases}
        missing_method = [m for m in _PAD_METHODS if m not in used_methods]
        missing_category = [c for c in CATEGORIES if c not in used_categories]
        if (
            len(raw_cases) >= MIN_CASES
            and len(used_methods) >= MIN_METHOD_COVERAGE
            and len(used_categories) >= MIN_CATEGORY_COVERAGE
        ):
            return raw_cases
        method = missing_method[0] if missing_method else _PAD_METHODS[i % len(_PAD_METHODS)]
        category = missing_category[0] if missing_category else CATEGORIES[i % len(CATEGORIES)]
        if method == "边界值":
            if profile.boundaries:
                b = profile.boundaries[0]
                v, u = _fmt_num(b.value), b.unit
                lo, hi = _fmt_num(b.value - 1), _fmt_num(b.value + 1)
                expected = (
                    f"条款「{b.text}」：输入 {v}{u}（恰好达到边界值）时被接受；"
                    f"条款「{b.text}」：输入 {lo}{u}（少 1）被拒绝，输入 {hi}{u}（多 1）被接受"
                )
            else:
                expected = (
                    "输入 1 件（恰好达到最小数量 1 件）时被接受；"
                    "两侧相邻值方向：输入 0 件（少 1）被拒绝，输入 2 件（多 1）被接受"
                )
        elif anchor:
            expected = (
                f"{anchor}：按{method}方法论验证{category}类输入并处理成功；"
                f"{anchor}：结果与{anchor}的条款定义一致"
            )
        else:
            expected = "系统行为符合需求规则定义，无异常与数据不一致"
        raw_cases.append(
            {
                "title": f"【{method}】补充用例 - {category}类覆盖（{i + 1}）",
                "precondition": "系统处于功能可用状态",
                "steps": (
                    f"1. 按{method}方法论准备{category}类测试数据{anchor}\n"
                    f"2. 执行目标操作{anchor}\n"
                    f"3. 观察并记录{anchor}的系统表现"
                ),
                "expected": expected,
                "priority": "P2" if category in ("异常", "边界") else "P1",
                "category": category,
                "method": method,
                "module": f"{module}/补充验证",
            }
        )
        i += 1


def generate(profile: DocumentProfile) -> list[dict]:
    """按规则生成用例原始 dict 列表（确定性，同一输入输出完全一致）。"""
    module = profile.module
    raw_cases: list[dict] = []

    # 规则 1：数字边界 → 边界值用例（控制规模，少而精）
    for b in profile.boundaries[:MAX_BOUNDARIES]:
        raw_cases.extend(_boundary_cases(b, module))

    # 规则 2：功能点 / 接口 → 等价类用例（控制规模，少而精）
    for f in profile.features[:MAX_FEATURES]:
        raw_cases.extend(_equivalence_cases(f, module))

    # 规则 3：主流程 → 场景法用例
    raw_cases.extend(_scenario_cases(profile))

    # 规则 5：多条件组合 → 判定表用例
    raw_cases.extend(_decision_table_cases(profile))

    # 规则 6：接口端点补齐（仅 OpenAPI）——补齐主流程未覆盖资源的场景 / 边界专项
    raw_cases.extend(_endpoint_cases(profile))

    # 规则 5：数量与覆盖度补齐
    return _pad_cases(raw_cases, profile)
