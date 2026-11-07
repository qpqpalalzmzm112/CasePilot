"""CLI eval / judge 命令集成测试（全部离线，输出断言中文报告与落盘）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from casepilot.cli import app

runner = CliRunner()
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
LOGIN_PRD = EXAMPLES / "prd_login.md"


class TestEvalCommand:
    def test_eval_writes_chinese_report(self, monkeypatch, tmp_path, offline_env):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["eval", "-i", str(LOGIN_PRD)])
        assert result.exit_code == 0
        assert "总分" in result.output
        assert "## 维度得分" in result.output
        assert "## 改进建议" in result.output
        report = tmp_path / "output" / "eval_report_prd_login.md"
        assert report.is_file(), "评分报告应落盘 output/eval_report_<样例名>.md"
        text = report.read_text(encoding="utf-8")
        for name in ("结构合法率", "方法论覆盖率", "边界数值化率", "基准要素重合度"):
            assert name in text

    def test_eval_deterministic(self, monkeypatch, tmp_path, offline_env):
        monkeypatch.chdir(tmp_path)
        first = runner.invoke(app, ["eval", "-i", str(LOGIN_PRD)])
        report = (tmp_path / "output" / "eval_report_prd_login.md").read_text(encoding="utf-8")
        second = runner.invoke(app, ["eval", "-i", str(LOGIN_PRD)])
        report2 = (tmp_path / "output" / "eval_report_prd_login.md").read_text(encoding="utf-8")
        assert first.exit_code == 0 and second.exit_code == 0
        assert report == report2, "同输入同分数同报告（确定性）"

    def test_eval_custom_golden(self, monkeypatch, tmp_path, offline_env):
        monkeypatch.chdir(tmp_path)
        golden = tmp_path / "my_golden.json"
        golden.write_text(
            '[{"title": "【等价类】登录主流程", "precondition": "账号已注册", '
            '"steps": "1. 输入账号\\n2. 点击登录", "expected": "登录成功跳转首页", '
            '"priority": "P0", "category": "功能", "method": "等价类", '
            '"module": "用户登录模块需求文档/登录功能"}]',
            encoding="utf-8",
        )
        result = runner.invoke(app, ["eval", "-i", str(LOGIN_PRD), "--golden", str(golden)])
        assert result.exit_code == 0
        assert "1 条人工整理基准用例" in result.output

    def test_eval_missing_golden_exits_1(self, monkeypatch, tmp_path, offline_env):
        monkeypatch.chdir(tmp_path)
        src = tmp_path / "unknown_doc.md"
        src.write_text("# 需求\n\n## 功能清单\n\n- 支持登录\n", encoding="utf-8")
        result = runner.invoke(app, ["eval", "-i", str(src)])
        assert result.exit_code == 1
        assert "未找到基准集" in result.output

    def test_eval_help_is_chinese(self):
        result = runner.invoke(app, ["eval", "--help"])
        assert "评测生成质量" in result.output
        assert "基准集" in result.output

    def test_evals_run_module_entry(self, monkeypatch, tmp_path, offline_env, capsys):
        """python -m evals.run 等价入口（薄封装）。"""
        from evals.run import main as run_main

        monkeypatch.chdir(tmp_path)
        run_main(["-i", str(LOGIN_PRD)])
        out = capsys.readouterr().out
        assert "总分" in out and "评分报告已保存" in out
        assert (tmp_path / "output" / "eval_report_prd_login.md").is_file()


class TestJudgeCommand:
    def _gen_cases(self, monkeypatch, tmp_path, offline_env):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["gen", "-i", str(LOGIN_PRD), "--emit", "xlsx", "-o", "cases.xlsx"])
        assert result.exit_code == 0
        return tmp_path / "cases.xlsx"

    def test_judge_offline_runs_with_scores_and_suggestions(self, monkeypatch, tmp_path, offline_env):
        cases = self._gen_cases(monkeypatch, tmp_path, offline_env)
        result = runner.invoke(app, ["judge", str(cases)])
        assert result.exit_code == 0
        assert "评审模式：规则降级" in result.output
        assert "总分" in result.output
        for dim in ("完整性", "方法论标注合理性", "优先级合理性", "可执行性"):
            assert dim in result.output
        assert "改进建议" in result.output
        assert result.output.count("用例「") >= 3, "至少 3 条引用具体用例标题的改进建议"

    def test_judge_output_file(self, monkeypatch, tmp_path, offline_env):
        cases = self._gen_cases(monkeypatch, tmp_path, offline_env)
        report = tmp_path / "judge_report.md"
        result = runner.invoke(app, ["judge", str(cases), "-o", str(report)])
        assert result.exit_code == 0
        assert report.is_file()
        assert "LLM-as-a-Judge" in report.read_text(encoding="utf-8")

    def test_judge_missing_file_exits_1(self, tmp_path, offline_env):
        result = runner.invoke(app, ["judge", str(tmp_path / "none.xlsx")])
        assert result.exit_code == 1
        assert "用例文件不存在" in result.output

    def test_judge_help_is_chinese(self):
        result = runner.invoke(app, ["judge", "--help"])
        assert "LLM-as-a-Judge" in result.output
        assert "规则评分降级" in result.output
