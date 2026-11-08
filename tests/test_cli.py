"""CLI 测试（typer CliRunner）：--version、gen --help 与离线生成。"""

from openpyxl import load_workbook
from typer.testing import CliRunner

from casepilot import __version__
from casepilot.cli import app
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, PETSTORE_OPENAPI

runner = CliRunner()


def _combined(result) -> str:
    """兼容 click 不同版本的输出捕获方式。"""
    out = result.output or ""
    try:
        out += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    return out


class TestVersion:
    def test_version_flag(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert f"casepilot {__version__}" in _combined(result)
        assert __version__ == "0.1.0"


class TestHelp:
    def test_root_help_in_chinese(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        text = _combined(result)
        assert "CasePilot" in text
        assert "测试用例" in text

    def test_gen_help_in_chinese(self):
        result = runner.invoke(app, ["gen", "--help"])
        assert result.exit_code == 0
        text = _combined(result)
        assert "输入文档路径" in text
        assert "导出格式" in text
        assert "输出文件路径" in text
        assert "禅道" in text
        assert "显示帮助信息并退出" in text  # 内置 --help 选项文案已本地化


class TestGenCommand:
    def test_gen_login_prd(self, tmp_path, offline_env):
        out = tmp_path / "login_cases.xlsx"
        result = runner.invoke(app, ["gen", "-i", str(LOGIN_PRD), "-o", str(out)])
        assert result.exit_code == 0, _combined(result)
        assert out.exists()
        assert "mock" in _combined(result)  # 中文日志提示当前 LLM 模式
        assert "导出完成" in _combined(result)
        wb = load_workbook(out)
        assert wb.sheetnames == ["用例"]

    def test_gen_all_samples(self, tmp_path, offline_env):
        for sample in (LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD):
            out = tmp_path / f"{sample.stem}.xlsx"
            result = runner.invoke(app, ["gen", "-i", str(sample), "-o", str(out)])
            assert result.exit_code == 0, _combined(result)
            ws = load_workbook(out)["用例"]
            assert ws.max_row >= 9  # 表头 + ≥8 条用例

    def test_gen_default_output_path(self, tmp_path, offline_env, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["gen", "-i", str(LOGIN_PRD)])
        assert result.exit_code == 0, _combined(result)
        default_out = tmp_path / "output" / "prd_login_cases.xlsx"
        assert default_out.exists()

    def test_gen_unsupported_format(self, tmp_path, offline_env):
        result = runner.invoke(
            app, ["gen", "-i", str(LOGIN_PRD), "--format", "json", "-o", str(tmp_path / "x.xlsx")]
        )
        assert result.exit_code == 1
        assert "暂不支持导出格式" in _combined(result)

    def test_gen_missing_input(self, tmp_path, offline_env):
        result = runner.invoke(app, ["gen", "-i", "不存在.md", "-o", str(tmp_path / "x.xlsx")])
        assert result.exit_code == 1
        assert "不存在" in _combined(result)

    def test_gen_unsupported_input_suffix(self, tmp_path, offline_env):
        docx = tmp_path / "需求.docx"
        docx.write_text("x", encoding="utf-8")
        result = runner.invoke(app, ["gen", "-i", str(docx), "-o", str(tmp_path / "x.xlsx")])
        assert result.exit_code == 1
        assert "暂不支持的输入格式" in _combined(result)

    def test_progress_and_mode_printed(self, tmp_path, offline_env):
        out = tmp_path / "b.xlsx"
        result = runner.invoke(app, ["gen", "-i", str(BOUNDARY_PRD), "-o", str(out)])
        text = _combined(result)
        assert "当前 LLM 模式" in text
        assert "确定性规则引擎" in text  # 降级提示
        assert "解析完成" in text
