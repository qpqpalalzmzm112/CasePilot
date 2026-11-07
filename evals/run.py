"""evals 运行入口：casepilot eval 命令的编排实现（薄封装）。

流程：输入文档 → LLM 网关生成用例（无 key 自动降级 mock）→ 对照 golden 基准集
4 维评分 → 中文评分报告落盘 output/eval_report_<样例名>.md。
评分结果确定性（同输入同分数），报告不含时间戳。

亦可直接运行：python -m evals.run -i examples/prd_login.md [--golden ...]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Union

from casepilot.core.generator import generate_from_file

from .metrics import evaluate, find_golden_path, load_golden


def run_eval(
    input_path: Union[str, Path],
    golden_path: Union[str, Path, None] = None,
    output_dir: Union[str, Path] = "output",
) -> tuple[str, Path]:
    """对单个输入文档执行评测，返回（报告文本, 报告落盘路径）。"""
    p = Path(input_path)
    result = generate_from_file(p)  # LLM 网关：有 key 走真实 LLM，否则确定性 mock

    golden_file = Path(golden_path) if golden_path else find_golden_path(p)
    golden = load_golden(golden_file)

    report = evaluate([c.model_dump() for c in result.cases], golden)
    text = report.format_report(
        input_name=str(p),
        golden_name=str(golden_file),
        golden_count=len(golden),
        case_count=len(result.cases),
        mode_note=f"生成模式：{result.mode}",
    )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"eval_report_{p.stem}.md"
    report_path.write_text(text, encoding="utf-8")
    return text, report_path


def main(argv: Optional[list[str]] = None) -> None:
    """python -m evals.run 入口（与 casepilot eval 命令等价）。"""
    parser = argparse.ArgumentParser(description="CasePilot 评测：生成用例对照 golden 基准集 4 维评分")
    parser.add_argument("-i", "--input", required=True, help="输入文档路径（PRD / OpenAPI）")
    parser.add_argument("--golden", default=None, help="golden 基准集路径，默认自动匹配 datasets/<输入名>_golden.json")
    args = parser.parse_args(argv)
    text, path = run_eval(args.input, args.golden)
    print(text)
    print(f"\n评分报告已保存：{path}")


if __name__ == "__main__":
    main()
