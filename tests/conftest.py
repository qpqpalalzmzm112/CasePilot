"""pytest 公共配置：样例路径与离线环境夹具（全部测试不出网）。"""

from pathlib import Path

import pytest

# 样例目录
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

LOGIN_PRD = EXAMPLES_DIR / "prd_login.md"
BOUNDARY_PRD = EXAMPLES_DIR / "prd_boundary.md"
PETSTORE_OPENAPI = EXAMPLES_DIR / "openapi_petstore.yaml"
SECURITY_PERF_PRD = EXAMPLES_DIR / "prd_security_perf.md"
NUMBERED_PRD = EXAMPLES_DIR / "prd_numbered.md"


@pytest.fixture
def offline_env(monkeypatch):
    """保证测试全程离线：清除 API key 相关环境变量，并屏蔽 Ollama 探测。"""
    monkeypatch.delenv("CASEPILOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CASEPILOT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("CASEPILOT_LLM_MODEL", raising=False)
    monkeypatch.delenv("CASEPILOT_JUDGE_MODEL", raising=False)
    monkeypatch.setattr("casepilot.core.llm.probe_ollama", lambda *args, **kwargs: False)


@pytest.fixture
def sample_case_data() -> dict:
    """一条合法的用例原始数据。"""
    return {
        "title": "验证登录成功",
        "precondition": "用户已注册且账号状态正常",
        "steps": "1. 打开登录页\n2. 输入账号密码\n3. 点击登录",
        "expected": "登录成功并跳转首页",
        "priority": "P0",
        "category": "功能",
        "method": "场景法",
        "module": "登录/场景验证",
    }
