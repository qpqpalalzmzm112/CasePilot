"""pytest 脚本骨架导出（核心差异化）。

把结构化测试用例导出为可被 pytest 正常收集运行的合法 Python 骨架文件：
- 文件头注释：来源文档、生成时间、用例总数、方法论 → pytest.mark 映射说明
- 每条用例一个 def test_<序号>_<slug>() 函数（函数名由中文用例标题 / 接口名生成，合法 Python 标识符）
- 函数内注释块：方法论、用例标题、所属模块、优先级 / 类型、前置条件、步骤、预期结果
- 方法论双标注：# 方法: xxx 注释 + @pytest.mark.<method_en> 自定义标记
  （等价类=equivalence、边界值=boundary、场景法=scenario、判定表=decision_table、
   安全测试=security、性能测试=performance）
- 函数体：pytest.skip("待实现") 占位 + TODO 注释

生成文件保证：
1. 通过 python -m py_compile（合法 UTF-8 Python）
2. pytest <文件> --collect-only 零收集错误（用例函数可全部被收集）
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Union

from ..core.schema import TestCase

# 方法论 → pytest 自定义 mark 英文映射（文件头注释同步说明）
# 迭代 6 扩展：安全测试 / 性能测试 → security / performance
METHOD_MARKS: dict[str, str] = {
    "等价类": "equivalence",
    "边界值": "boundary",
    "场景法": "scenario",
    "判定表": "decision_table",
    "安全测试": "security",
    "性能测试": "performance",
}

# slug 最大长度（避免函数名过长）
_SLUG_MAX_LEN = 40


def _slugify(title: str, max_len: int = _SLUG_MAX_LEN) -> str:
    """把用例标题转为合法 Python 标识符片段（保留中文 / 数字 / 字母，其余转下划线）。

    中文字符本身是合法 Python 标识符字符（PEP 3131），生成的函数名可直接被
    pytest 收集与 py_compile 编译。
    """
    text = re.sub(r"\W+", "_", title, flags=re.UNICODE).strip("_")
    if len(text) > max_len:
        text = text[:max_len].rstrip("_")
    return text or "case"


def _unique_name(index: int, slug: str, used: set[str]) -> str:
    """生成唯一函数名：test_<序号三位>_<slug>，冲突时追加序号后缀。"""
    base = f"test_{index:03d}_{slug}"
    name = base
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def _comment_block(label: str, text: str, indent: str = "    ") -> list[str]:
    """把多行文本渲染为缩进的 # 注释块（每行一条注释）。"""
    lines = text.splitlines() or [""]
    out = [f"{indent}# {label}: {lines[0].strip()}"]
    for extra in lines[1:]:
        out.append(f"{indent}#   {extra.strip()}")
    return out


def build_skeleton(
    cases: Iterable[TestCase], source: str = "", generated_at: Union[str, datetime, None] = None
) -> str:
    """把用例集渲染为 pytest 骨架文件文本（合法 Python）。"""
    case_list = list(cases)
    if generated_at is None:
        generated_at = datetime.now()
    if isinstance(generated_at, datetime):
        generated_at = generated_at.strftime("%Y-%m-%d %H:%M:%S")

    header = [
        "# 本文件由 CasePilot 自动生成（pytest 脚本骨架），请按 TODO 逐条补充自动化实现。",
        f"# 来源文档：{source or '（未提供）'}",
        f"# 生成时间：{generated_at}",
        f"# 用例总数：{len(case_list)}",
        "#",
        "# 方法论 → pytest.mark 自定义标记映射：",
    ]
    for method, mark in METHOD_MARKS.items():
        header.append(f"#   {method} → pytest.mark.{mark}")
    header.extend(
        [
            "# 提示：可在 pytest 配置（pytest.ini / pyproject.toml [tool.pytest.ini_options] markers）",
            "#       中注册上述标记，消除 PytestUnknownMarkWarning 告警。",
            "#",
            "# 使用方式：",
            "#   pytest 本文件 --collect-only   # 校验骨架可被 pytest 正常收集",
            "#   pytest 本文件 -v               # 运行（未实现用例统一 skip）",
            "",
            "import pytest",
            "",
            "",
        ]
    )

    used_names: set[str] = set()
    body: list[str] = []
    for index, case in enumerate(case_list, 1):
        mark = METHOD_MARKS.get(case.method, "scenario")
        func_name = _unique_name(index, _slugify(case.title), used_names)

        block = [
            f"# 方法: {case.method}{'（设计假设）' if case.assumption else ''}",
            f"@pytest.mark.{mark}",
            f"def {func_name}():",
        ]
        block += _comment_block("用例标题", case.title)
        block += _comment_block("所属模块", case.module)
        block.append(f"    # 优先级: {case.priority}    用例类型: {case.category}")
        block += _comment_block("前置条件", case.precondition)
        block += _comment_block("步骤", case.steps)
        block += _comment_block("预期结果", case.expected)
        block.append("    # TODO: 待补充自动化脚本、测试数据与断言")
        block.append('    pytest.skip("待实现")')
        body.append("\n".join(block))

    return "\n".join(header) + "\n\n\n".join(body) + "\n"


def export_pytest_skeleton(
    cases: Iterable[TestCase],
    output_path: Union[str, Path],
    source: str = "",
    generated_at: Union[str, datetime, None] = None,
) -> Path:
    """把用例集导出为 pytest 脚本骨架 .py 文件，返回输出路径。"""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_skeleton(cases, source=source, generated_at=generated_at), encoding="utf-8")
    return out
