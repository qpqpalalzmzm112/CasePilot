"""CLI 测试（typer CliRunner）：--version、gen/api --help、--emit 三格式与离线生成。"""

import py_compile
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
        assert __version__ == "1.0.0"


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

    def test_coverage_report_printed(self, tmp_path, offline_env):
        out = tmp_path / "b.xlsx"
        result = runner.invoke(app, ["gen", "-i", str(LOGIN_PRD), "-o", str(out)])
        text = _combined(result)
        assert "覆盖维度检查：共" in text
        assert "覆盖维度检查结论：达标" in text


class TestEmitFormats:
    """--emit xlsx|pytest|md 三格式（UP-1 / UP-2）。"""

    def test_emit_pytest_default_path(self, tmp_path, offline_env, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["gen", "-i", str(LOGIN_PRD), "--emit", "pytest"])
        assert result.exit_code == 0, _combined(result)
        out = tmp_path / "output" / "prd_login_test_skeleton.py"
        assert out.exists()
        py_compile.compile(str(out), doraise=True)  # 合法 Python
        assert "导出完成" in _combined(result)

    def test_emit_md_default_path(self, tmp_path, offline_env, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["gen", "-i", str(BOUNDARY_PRD), "--emit", "md"])
        assert result.exit_code == 0, _combined(result)
        out = tmp_path / "output" / "prd_boundary_cases.md"
        assert out.exists()
        text = out.read_text(encoding="utf-8")
        assert text.startswith("# 用例集：")
        assert "## 方法论统计" in text

    def test_api_emit_pytest(self, tmp_path, offline_env):
        out = tmp_path / "pet.py"
        result = runner.invoke(
            app, ["api", "-i", str(PETSTORE_OPENAPI), "--emit", "pytest", "-o", str(out)]
        )
        assert result.exit_code == 0, _combined(result)
        assert out.exists()
        py_compile.compile(str(out), doraise=True)
        text = out.read_text(encoding="utf-8")
        assert "# 方法:" in text
        assert 'pytest.skip("待实现")' in text

    def test_api_emit_md_and_xlsx(self, tmp_path, offline_env):
        md_out = tmp_path / "pet.md"
        result = runner.invoke(
            app, ["api", "-i", str(PETSTORE_OPENAPI), "--emit", "md", "-o", str(md_out)]
        )
        assert result.exit_code == 0, _combined(result)
        assert md_out.read_text(encoding="utf-8").startswith("# 用例集：")

        xlsx_out = tmp_path / "pet.xlsx"
        result = runner.invoke(app, ["api", "-i", str(PETSTORE_OPENAPI), "-o", str(xlsx_out)])
        assert result.exit_code == 0, _combined(result)
        assert xlsx_out.exists()  # api 默认同样为 xlsx

    def test_api_rejects_prd_input(self, tmp_path, offline_env):
        result = runner.invoke(
            app, ["api", "-i", str(LOGIN_PRD), "--emit", "pytest", "-o", str(tmp_path / "x.py")]
        )
        assert result.exit_code == 1
        assert "仅支持 OpenAPI" in _combined(result)

    def test_api_missing_input(self, tmp_path, offline_env):
        result = runner.invoke(app, ["api", "-i", "不存在.yaml", "-o", str(tmp_path / "x.xlsx")])
        assert result.exit_code == 1
        assert "不存在" in _combined(result)

    def test_unsupported_emit_value(self, tmp_path, offline_env):
        result = runner.invoke(
            app, ["gen", "-i", str(LOGIN_PRD), "--emit", "json", "-o", str(tmp_path / "x")]
        )
        assert result.exit_code == 1
        assert "暂不支持导出格式" in _combined(result)

    def test_format_alias_backward_compatible(self, tmp_path, offline_env):
        """迭代 1 的 --format 用法保持可用（等价于 --emit）。"""
        out = tmp_path / "x.xlsx"
        result = runner.invoke(
            app, ["gen", "-i", str(LOGIN_PRD), "--format", "xlsx", "-o", str(out)]
        )
        assert result.exit_code == 0, _combined(result)
        assert out.exists()

    def test_gen_help_mentions_three_formats(self):
        result = runner.invoke(app, ["gen", "--help"])
        text = _combined(result)
        assert "pytest" in text
        assert "md" in text
        assert "禅道" in text


class TestZentaoImportReport:
    """导入前校验闭环：xlsx 导出后 CLI 输出中文校验报告（UP-2）。"""

    def test_xlsx_export_prints_importable_report(self, tmp_path, offline_env):
        out = tmp_path / "cases.xlsx"
        result = runner.invoke(app, ["gen", "-i", str(LOGIN_PRD), "-o", str(out)])
        text = _combined(result)
        assert "禅道导入前校验" in text
        assert "校验结论：可导入" in text

    def test_pytest_and_md_skip_import_report(self, tmp_path, offline_env):
        result = runner.invoke(
            app, ["gen", "-i", str(LOGIN_PRD), "--emit", "md", "-o", str(tmp_path / "c.md")]
        )
        assert "禅道导入前校验" not in _combined(result)
