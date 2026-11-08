"""pytest 脚本骨架导出测试：合法 Python、可编译、可被 pytest 零错误收集（UP-1）。"""

import ast
import os
import py_compile
import re
import subprocess
import sys

import pytest

from casepilot.core.generator import generate_from_file
from casepilot.exporters.pytest_skeleton import (
    METHOD_MARKS,
    build_skeleton,
    export_pytest_skeleton,
)
from tests.conftest import BOUNDARY_PRD, LOGIN_PRD, PETSTORE_OPENAPI

SAMPLES = [LOGIN_PRD, PETSTORE_OPENAPI, BOUNDARY_PRD]

# 函数名规范：test_<三位序号>_<slug>
_FUNC_RE = re.compile(r"^test_\d{3}_")


def _skeleton(path, tmp_path, name):
    """对样例离线生成骨架并返回输出路径。"""
    result = generate_from_file(path, mode="mock")
    return export_pytest_skeleton(
        result.cases, tmp_path / name, source=str(path), generated_at="2026-01-01 00:00:00"
    ), result


class TestSkeletonContent:
    def test_header_contains_source_and_time(self, tmp_path):
        out, result = _skeleton(PETSTORE_OPENAPI, tmp_path, "s.py")
        text = out.read_text(encoding="utf-8")
        assert "来源文档：" in text
        assert str(PETSTORE_OPENAPI) in text
        assert "生成时间：2026-01-01 00:00:00" in text
        assert f"用例总数：{len(result.cases)}" in text

    def test_header_documents_mark_mapping(self, tmp_path):
        out, _ = _skeleton(PETSTORE_OPENAPI, tmp_path, "s.py")
        text = out.read_text(encoding="utf-8")
        for method, mark in METHOD_MARKS.items():
            assert f"{method} → pytest.mark.{mark}" in text

    def test_method_marks_and_comment(self, tmp_path):
        out, result = _skeleton(LOGIN_PRD, tmp_path, "s.py")
        text = out.read_text(encoding="utf-8")
        # 标记与注释按用例实际使用的方法论断言（方法论枚举含安全测试 / 性能测试，
        # 登录样例不含安全 / 性能需求，不应出现对应标记）
        used = {c.method for c in result.cases}
        for method in used:
            assert f"@pytest.mark.{METHOD_MARKS[method]}" in text
            assert f"# 方法: {method}" in text
        # 每个用例的方法论都写入注释
        assert text.count("# 方法: ") == len(result.cases)

    def test_comment_block_elements(self, tmp_path):
        out, result = _skeleton(BOUNDARY_PRD, tmp_path, "s.py")
        text = out.read_text(encoding="utf-8")
        assert text.count("# 前置条件:") == len(result.cases)
        assert text.count("# 步骤:") == len(result.cases)
        assert text.count("# 预期结果:") == len(result.cases)
        assert text.count("# 用例标题:") == len(result.cases)
        assert text.count("# 所属模块:") == len(result.cases)
        assert 'pytest.skip("待实现")' in text
        assert text.count('pytest.skip("待实现")') == len(result.cases)
        assert "# TODO: 待补充自动化脚本、测试数据与断言" in text

    def test_function_names_valid_and_unique(self, tmp_path):
        out, result = _skeleton(PETSTORE_OPENAPI, tmp_path, "s.py")
        tree = ast.parse(out.read_text(encoding="utf-8"))
        names = [
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        ]
        assert len(names) == len(result.cases)
        assert len(set(names)) == len(names), "函数名必须唯一"
        for name in names:
            assert _FUNC_RE.match(name), f"函数名不规范：{name}"
            assert name.isidentifier(), f"不是合法 Python 标识符：{name}"

    def test_petstore_min_ten_functions(self, tmp_path):
        out, _ = _skeleton(PETSTORE_OPENAPI, tmp_path, "s.py")
        tree = ast.parse(out.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert len(funcs) >= 10

    def test_import_pytest_present(self, tmp_path):
        out, _ = _skeleton(LOGIN_PRD, tmp_path, "s.py")
        tree = ast.parse(out.read_text(encoding="utf-8"))
        imports = [
            n for n in ast.walk(tree) if isinstance(n, ast.Import)
        ]
        assert any("pytest" in alias.name for n in imports for alias in n.names)

    def test_slugify_handles_unsafe_chars(self):
        from casepilot.exporters.pytest_skeleton import _slugify

        assert _slugify("【边界值】密码长度 8-20 位").isidentifier()
        assert _slugify("!!!") == "case"
        assert len(_slugify("长" * 100)) <= 40

    def test_build_skeleton_deterministic(self):
        result = generate_from_file(LOGIN_PRD, mode="mock")
        first = build_skeleton(result.cases, source="a.md", generated_at="2026-01-01 00:00:00")
        second = build_skeleton(result.cases, source="a.md", generated_at="2026-01-01 00:00:00")
        assert first == second


class TestCompileAndCollect:
    def _sub_env(self):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"  # 避免 Windows 管道下中文输出编码问题
        return env

    def test_all_samples_compile(self, tmp_path):
        for i, sample in enumerate(SAMPLES):
            out, _ = _skeleton(sample, tmp_path, f"s{i}.py")
            py_compile.compile(str(out), doraise=True)  # 不抛异常即合法 Python

    def test_pytest_collect_only_zero_error(self, tmp_path):
        outs = []
        for i, sample in enumerate(SAMPLES):
            out, _ = _skeleton(sample, tmp_path, f"s{i}.py")
            outs.append(out)

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider",
             "--disable-warnings", *[str(p) for p in outs]],
            capture_output=True,
            text=True,
            env=self._sub_env(),
            cwd=str(tmp_path.parent),
        )
        assert proc.returncode == 0, f"收集失败：\n{proc.stdout}\n{proc.stderr}"
        combined = proc.stdout + proc.stderr
        assert "error" not in combined.lower(), f"存在收集错误：\n{combined}"
        # 收集到的用例函数（node id / 函数条目均含 test_<序号>_ 前缀）
        collected = len(re.findall(r"test_\d{3}_", combined))
        assert collected >= 10, f"收集数量不足：\n{combined}"

    def test_run_skeleton_skips_pending(self, tmp_path):
        out, _ = _skeleton(BOUNDARY_PRD, tmp_path, "s.py")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(out), "-q", "-rs", "-p", "no:cacheprovider",
             "--disable-warnings"],
            capture_output=True,
            text=True,
            env=self._sub_env(),
            cwd=str(tmp_path.parent),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        combined = proc.stdout + proc.stderr
        assert "待实现" in combined  # skip 原因可见
        assert re.search(r"skipped", combined)
