"""确定性规则式 mock 生成器（无 key / 无 Ollama 时的最终降级方案）。

设计原则：同一输入必须输出完全相同的结果（不含随机数、不含时间戳）。
生成规则（与 core/methodology.py 的方法论模板保持一致）：
1. 数字 / 比较词边界（≥/≤/不超过/至少/超过 等）→ 边界值用例（取边界值与边界 ±1）
2. 功能点 / 动宾短语 → 等价类用例（有效类 + 无效类）
3. 主流程操作序列 → 场景法用例（基本流 / 备选流 / 异常流）
4. 优先级规则：主流程 P0、有效等价类与边界 P1、异常 / 无效类 P2
5. 多条件组合 → 判定表用例；最终保证每文档 ≥8 条用例，
   method 覆盖 ≥3 种、category 覆盖 ≥3 种
"""

from __future__ import annotations

from .schema import CATEGORIES, METHODS, Boundary, DocumentProfile, Feature

# 用例数量与覆盖度下限（UP-3 验收标准）
MIN_CASES = 8
MIN_METHOD_COVERAGE = 3
MIN_CATEGORY_COVERAGE = 3

# 单文档生成规模上限（少而精：避免机械用例灌水）
MAX_BOUNDARIES = 5
MAX_FEATURES = 6


def _fmt_num(value: float) -> str:
    """把数值格式化为整洁的中文文本（整数不带小数点）。"""
    return f"{value:g}"


def _boundary_cases(b: Boundary, module: str) -> list[dict]:
    """规则 1：数字边界 → 边界值用例（边界值本身 P1、边界 ±1 P2）。

    预期结果必须引用具体数值与两侧相邻值（v-1 / v / v+1）并写明方向
    （接受 / 拒绝 / 触发），不得使用通用套话（UP-3 质量门禁）。
    """
    value = _fmt_num(b.value)
    unit = b.unit
    lo = _fmt_num(b.value - 1)
    hi = _fmt_num(b.value + 1)
    if b.kind == "range_min":
        inside, outside = b.value, b.value - 1
        inside_desc, outside_desc = f"最小值 {_fmt_num(inside)}{unit}", f"低于最小值 {_fmt_num(outside)}{unit}"
        inside_expected = (
            f"输入 {value}{unit}（恰好达到最小值）时被接受，校验通过并正常处理；"
            f"两侧相邻值方向：输入 {lo}{unit}（少 1）被拒绝并提示，输入 {hi}{unit}（多 1）正常通过"
        )
        outside_expected = (
            f"输入 {lo}{unit}（低于最小值 {value}{unit}）时被拒绝并给出明确中文提示；"
            f"两侧相邻值对照：输入 {value}{unit}（最小值）被接受，输入 {hi}{unit}（多 1）正常通过"
        )
    elif b.kind == "range_max":
        inside, outside = b.value, b.value + 1
        inside_desc, outside_desc = f"最大值 {_fmt_num(inside)}{unit}", f"超过最大值 {_fmt_num(outside)}{unit}"
        inside_expected = (
            f"输入 {value}{unit}（恰好达到最大值）时被接受，校验通过并正常处理；"
            f"两侧相邻值方向：输入 {lo}{unit}（少 1）正常通过，输入 {hi}{unit}（多 1）被拒绝并提示"
        )
        outside_expected = (
            f"输入 {hi}{unit}（超过最大值 {value}{unit}）时被拒绝并给出明确中文提示；"
            f"两侧相邻值对照：输入 {lo}{unit}（少 1）正常通过，输入 {value}{unit}（最大值）被接受"
        )
    else:
        inside, outside = b.value, b.value - 1
        inside_desc = f"恰好达到阈值 {value}{unit}"
        outside_desc = f"阈值相邻值 {_fmt_num(outside)}{unit} / {_fmt_num(b.value + 1)}{unit}"
        inside_expected = (
            f"输入 {value}{unit}（恰好达到阈值）时被接受并按规则处理；"
            f"两侧相邻值方向：输入 {lo}{unit}（阈值少 1）未达阈值不触发，"
            f"输入 {hi}{unit}（阈值多 1）触发阈值限制并提示"
        )
        outside_expected = (
            f"输入 {lo}{unit} 与 {hi}{unit}（阈值 {value}{unit} 的两侧相邻值）按方向分别处理："
            f"{lo}{unit}（少 1）未达阈值不触发限制，{hi}{unit}（多 1）触发阈值限制被拒绝并提示"
        )

    return [
        {
            "title": f"【边界值】{b.text} - 取边界值{inside_desc}",
            "precondition": "系统处于功能可用状态，测试数据已准备就绪",
            "steps": (
                f"1. 准备满足前置条件的测试数据\n"
                f"2. 将输入调整为{inside_desc}\n"
                f"3. 提交操作并观察系统处理"
            ),
            "expected": inside_expected,
            "priority": "P1",
            "category": "边界",
            "method": "边界值",
            "module": f"{module}/边界验证",
        },
        {
            "title": f"【边界值】{b.text} - 取越界值{outside_desc}",
            "precondition": "系统处于功能可用状态，测试数据已准备就绪",
            "steps": (
                f"1. 准备满足前置条件的测试数据\n"
                f"2. 将输入调整为{outside_desc}\n"
                f"3. 提交操作并观察系统处理"
            ),
            "expected": outside_expected,
            "priority": "P2",
            "category": "边界",
            "method": "边界值",
            "module": f"{module}/边界验证",
        },
    ]


def _equivalence_cases(f: Feature, module: str) -> list[dict]:
    """规则 2：功能点 / 接口 → 等价类用例（有效类 P1 + 无效类 P2）。"""
    valid_category = "接口" if f.kind == "api" else "功能"
    return [
        {
            "title": f"【等价类】{f.name} - 有效等价类验证",
            "precondition": f"已满足「{f.name}」的基本使用条件",
            "steps": (
                f"1. 准备合法有效的输入数据\n"
                f"2. 执行「{f.name}」操作\n"
                f"3. 观察系统响应与最终状态"
            ),
            "expected": "系统接受有效等价类输入，功能执行成功，返回结果符合预期",
            "priority": "P1",
            "category": valid_category,
            "method": "等价类",
            "module": f"{module}/功能验证",
        },
        {
            "title": f"【等价类】{f.name} - 无效等价类验证",
            "precondition": f"已满足「{f.name}」的基本使用条件",
            "steps": (
                f"1. 准备非法 / 缺失 / 超范围的无效输入数据\n"
                f"2. 执行「{f.name}」操作\n"
                f"3. 观察系统响应与校验提示"
            ),
            "expected": "系统拒绝无效等价类输入，给出明确中文错误提示，数据不被写入",
            "priority": "P2",
            "category": "异常",
            "method": "等价类",
            "module": f"{module}/异常验证",
        },
    ]


def _scenario_cases(flows: list[str], module: str) -> list[dict]:
    """规则 3：主流程操作序列 → 场景法用例（基本流 / 备选流 / 异常流）。"""
    flow_steps = "\n".join(f"{i}. {step}" for i, step in enumerate(flows, 1))
    flow_line = " → ".join(flows[:3])
    return [
        {
            "title": f"【场景法】基本流：{flow_line}",
            "precondition": "初始状态正常，测试账号 / 数据已准备就绪",
            "steps": flow_steps,
            "expected": "各步骤顺序执行成功，业务目标顺利达成，最终状态正确",
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
                "2. 在中间步骤跳过可选项或临时中断操作\n"
                "3. 恢复操作并继续执行剩余步骤"
            ),
            "expected": "系统允许合法的可选分支，恢复后流程可继续完成，数据状态保持一致",
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
                "2. 在关键步骤注入异常（网络中断 / 服务不可用 / 数据冲突）\n"
                "3. 观察系统的错误提示与数据回滚情况"
            ),
            "expected": "系统给出明确错误提示，事务回滚或数据保持一致，不产生脏数据",
            "priority": "P2",
            "category": "异常",
            "method": "场景法",
            "module": f"{module}/异常验证",
        },
    ]


def _decision_table_cases(profile: DocumentProfile) -> list[dict]:
    """规则 5：多条件组合 → 判定表用例（条件全成立 / 部分不成立）。"""
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
    cond_lines = "\n".join(f"{i}. {c}" for i, c in enumerate(conditions, 1))
    return [
        {
            "title": "【判定表】多条件组合 - 全部条件成立",
            "precondition": "测试环境与数据满足全部条件的准备要求",
            "steps": (
                f"1. 按判定表逐项确认条件：\n{cond_lines}\n"
                "2. 条件全部成立时执行目标操作\n"
                "3. 核对系统动作与判定表期望动作一致"
            ),
            "expected": "系统按判定表规则执行对应动作，结果与规则定义一致",
            "priority": "P1",
            "category": category,
            "method": "判定表",
            "module": f"{module}/组合验证",
        },
        {
            "title": "【判定表】多条件组合 - 部分条件不成立",
            "precondition": "测试环境与数据满足部分条件的准备要求",
            "steps": (
                "1. 构造仅部分条件成立的输入组合（至少一个条件不成立）\n"
                "2. 执行目标操作\n"
                "3. 核对系统动作与判定表期望动作一致"
            ),
            "expected": "系统按判定表规则拒绝或走备选动作，并给出明确提示",
            "priority": "P2",
            "category": "异常",
            "method": "判定表",
            "module": f"{module}/组合验证",
        },
    ]


def _pad_cases(raw_cases: list[dict], module: str) -> list[dict]:
    """确定性补齐：保证 ≥8 条用例，且 method / category 覆盖达标。

    补齐落到「边界值」时，预期结果同样必须引用具体数值与两侧相邻值
    （UP-3：边界值用例禁止通用套话），锚点取 1 件（0/1/2 三个确定值）。
    """
    i = 0
    while True:
        used_methods = {c["method"] for c in raw_cases}
        used_categories = {c["category"] for c in raw_cases}
        missing_method = [m for m in METHODS if m not in used_methods]
        missing_category = [c for c in CATEGORIES if c not in used_categories]
        if (
            len(raw_cases) >= MIN_CASES
            and len(used_methods) >= MIN_METHOD_COVERAGE
            and len(used_categories) >= MIN_CATEGORY_COVERAGE
        ):
            return raw_cases
        method = missing_method[0] if missing_method else METHODS[i % len(METHODS)]
        category = missing_category[0] if missing_category else CATEGORIES[i % len(CATEGORIES)]
        if method == "边界值":
            expected = (
                "输入 1 件（恰好达到最小数量 1 件）时被接受；"
                "两侧相邻值方向：输入 0 件（少 1）被拒绝并提示，输入 2 件（多 1）正常通过"
            )
        else:
            expected = "系统行为符合需求规则定义，无异常与数据不一致"
        raw_cases.append(
            {
                "title": f"【{method}】补充用例 - {category}类覆盖（{i + 1}）",
                "precondition": "系统处于功能可用状态",
                "steps": (
                    f"1. 按{method}方法论准备{category}类测试数据\n"
                    "2. 执行目标操作\n"
                    "3. 观察并记录系统表现"
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
    flows = profile.flows or [f"完成「{module}」的核心操作"]
    raw_cases.extend(_scenario_cases(flows, module))

    # 规则 5：多条件组合 → 判定表用例
    raw_cases.extend(_decision_table_cases(profile))

    # 规则 5：数量与覆盖度补齐
    return _pad_cases(raw_cases, module)
