"""迭代 4（UP-1）验收测试：CI workflow YAML 语法合法与关键字段断言。

用 Python yaml 解析 .github/workflows/ci.yml，断言：
触发条件（push / pull_request）、Python 版本矩阵（3.10 / 3.12）、
测试命令（pytest）与覆盖率门禁（--cov-fail-under=80）、仅用官方 action。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "README.md"


def _load_workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "workflow 应解析为 mapping"
    return data


def _triggers(data: dict) -> dict:
    # PyYAML（YAML 1.1）会把裸键 on 解析为布尔 True，两种写法都兼容
    trigger = data.get("on", data.get(True))
    assert isinstance(trigger, dict), "触发条件应为 mapping"
    return trigger


class TestWorkflowYaml:
    def test_yaml_parses(self):
        data = _load_workflow()
        assert "jobs" in data

    def test_trigger_on_push_and_pull_request(self):
        trigger = _triggers(_load_workflow())
        assert set(trigger) == {"push", "pull_request"}, f"触发条件应为 push / pull_request：{set(trigger)}"

    def test_python_version_matrix(self):
        job = _load_workflow()["jobs"]["test"]
        matrix = job["strategy"]["matrix"]
        assert matrix["python-version"] == ["3.10", "3.12"]
        assert job["strategy"].get("fail-fast") is False

    def test_steps_use_official_actions_only(self):
        steps = _load_workflow()["jobs"]["test"]["steps"]
        uses = [s.get("uses", "") for s in steps]
        assert any(u.startswith("actions/checkout@") for u in uses)
        assert any(u.startswith("actions/setup-python@") for u in uses)
        # 不引入第三方覆盖率上传服务（如 codecov / coveralls）
        for s in steps:
            if "uses" in s:
                assert s["uses"].startswith("actions/"), f"只允许官方 actions/*：{s['uses']}"

    def test_install_dev_dependencies(self):
        steps = _load_workflow()["jobs"]["test"]["steps"]
        runs = "\n".join(s.get("run", "") for s in steps)
        assert 'pip install -e ".[dev]"' in runs

    def test_pytest_command_with_coverage_gate(self):
        steps = _load_workflow()["jobs"]["test"]["steps"]
        runs = "\n".join(s.get("run", "") for s in steps)
        assert "pytest --cov=casepilot --cov=evals --cov-fail-under=80" in runs

    def test_no_extra_secrets_referenced(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "secrets." not in text, "CI 只用 GitHub 内置 GITHUB_TOKEN，不配置额外 secrets"


class TestReadmeBadges:
    def test_badge_line_has_ci_and_coverage(self):
        text = README.read_text(encoding="utf-8")
        lines = text.splitlines()
        # 徽章行位于标题下方前几行内
        head = "\n".join(lines[:8])
        assert "img.shields.io/badge/coverage-" in head, "徽章行应含 shields.io 静态覆盖率徽章"
        assert "badge.svg" in head or "img.shields.io" in head, "徽章行应含 CI 状态徽章"

    def test_coverage_badge_format(self):
        text = README.read_text(encoding="utf-8")
        m = re.search(r"img\.shields\.io/badge/coverage-(\d+(?:\.\d+)?)%25-brightgreen", text)
        assert m, "覆盖率徽章应为 shields.io 静态样式（如 coverage-96%25-brightgreen）"
        value = float(m.group(1))
        assert 80.0 <= value <= 100.0, f"徽章覆盖率数字异常：{value}"
