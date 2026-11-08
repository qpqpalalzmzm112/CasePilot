"""测试方法论提示词模板。

CasePilot 的核心差异化之一：显式测试方法论标注。
四种方法论（等价类 / 边界值 / 场景法 / 判定表）各自定义了用例设计规则，
既用于 mock 规则引擎的生成逻辑，也用于真实 LLM 调用时的 system prompt。
"""

from __future__ import annotations

from .schema import DocumentProfile, METHODS

# 每种方法论的设计规则模板（中文）
METHOD_TEMPLATES: dict[str, str] = {
    "等价类": (
        "等价类划分：把输入域划分为若干等价类，"
        "每个功能点至少覆盖一个有效等价类（正常输入）与一个无效等价类（非法输入），"
        "有效类用例优先级 P1、类型「功能」，无效类用例优先级 P2、类型「异常」。"
    ),
    "边界值": (
        "边界值分析：针对需求中的数字与比较词（至少/不超过/超过/满/最多等），"
        "取边界值本身与边界值 ±1 设计用例；"
        "区间（如 8-20）取最小值、最大值与两端 ±1；"
        "边界值用例类型「边界」，取值有效为 P1、越界为 P2。"
    ),
    "场景法": (
        "场景法：梳理主流程操作序列，设计基本流（优先级 P0、类型「功能」）、"
        "备选流（优先级 P1、类型「功能」）与异常流（优先级 P2、类型「异常」）三类用例。"
    ),
    "判定表": (
        "判定表：针对多条件组合的业务规则，列出条件桩与动作桩的组合，"
        "为关键组合设计用例，覆盖条件同时成立 / 部分成立 / 全不成立的情况。"
    ),
}

# 优先级规则（与 mock 引擎保持一致）
PRIORITY_RULES = (
    "优先级规则：主流程基本流 P0；有效等价类与边界值用例 P1；"
    "异常 / 无效类 / 越界用例 P2；辅助场景 P3。"
)

# 用例结构要求（与 core/schema.py 的 TestCase 严格对应）
SCHEMA_REQUIREMENT = (
    "每条用例输出以下 JSON 字段："
    "title（用例标题）、precondition（前置条件）、"
    "steps（操作步骤，多步骤用 \"1. …\\n2. …\" 换行形式）、expected（预期结果）、"
    "priority（P0|P1|P2|P3）、category（功能|接口|异常|边界）、"
    "method（等价类|边界值|场景法|判定表）、module（所属模块，用 \"父模块/子模块\" 路径）。"
)


def build_system_prompt() -> str:
    """构建真实 LLM 调用的 system prompt：方法论模板 + 结构要求 + 输出格式。"""
    parts = ["你是资深测试用例设计专家，请严格按照以下测试方法论设计测试用例。"]
    for name in METHODS:
        parts.append(f"【{name}】{METHOD_TEMPLATES[name]}")
    parts.append(PRIORITY_RULES)
    parts.append(SCHEMA_REQUIREMENT)
    parts.append(
        "输出要求：只输出一个 JSON 数组，数组元素为用例对象，不要输出任何解释文字或 markdown 代码块；"
        "每条用例的 method 字段必须非空且取值合法；"
        "用例总数不少于 8 条，method 至少覆盖 3 种，category 至少覆盖 3 种。"
    )
    return "\n".join(parts)


def build_user_prompt(profile: DocumentProfile) -> str:
    """构建真实 LLM 调用的 user prompt：文档解析要点摘要。"""
    return (
        "请基于以下需求文档要点设计测试用例：\n"
        f"{profile.summary_text()}\n"
        "请输出 JSON 数组。"
    )
