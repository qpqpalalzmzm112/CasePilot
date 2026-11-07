"""LLM 网关双模式与降级决策测试（UP-2，全程离线，httpx 全部替身）。"""

import json

import pytest

from casepilot.core import llm
from casepilot.core.llm import LLMGateway
from casepilot.core.schema import DocumentProfile


def _profile() -> DocumentProfile:
    return DocumentProfile(source="a.md", source_type="prd", module="登录模块", flows=["打开登录页"])


def _valid_raw_case(title: str = "用例") -> dict:
    """一条结构合法的用例原始数据（可通过 pydantic 校验）。"""
    return {
        "title": title,
        "precondition": "前置条件",
        "steps": "1. 步骤一\n2. 步骤二",
        "expected": "预期结果",
        "priority": "P0",
        "category": "功能",
        "method": "场景法",
        "module": "登录模块/场景验证",
    }


class FakeResponse:
    """httpx 响应替身。"""

    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class TestDecideMode:
    @pytest.mark.parametrize(
        "has_key, ollama_reachable, expected",
        [
            (True, True, "openai"),
            (True, False, "openai"),
            (False, True, "ollama"),
            (False, False, "mock"),
        ],
    )
    def test_decision_matrix(self, has_key, ollama_reachable, expected):
        assert llm.decide_mode(has_key, ollama_reachable) == expected


class TestEnvConfig:
    def test_get_api_key_prefers_casepilot_var(self, monkeypatch):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "casepilot-key")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        assert llm.get_api_key() == "casepilot-key"

    def test_get_api_key_fallback_openai(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        assert llm.get_api_key() == "openai-key"

    def test_get_api_key_none_without_env(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert llm.get_api_key() is None

    def test_get_base_url_override(self, monkeypatch):
        monkeypatch.setenv("CASEPILOT_LLM_BASE_URL", "https://my-proxy.example.com/v1/")
        assert llm.get_base_url("openai") == "https://my-proxy.example.com/v1"

    def test_get_base_url_defaults(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_BASE_URL", raising=False)
        assert llm.get_base_url("openai") == llm.DEFAULT_OPENAI_BASE_URL
        assert llm.get_base_url("ollama") == llm.DEFAULT_OLLAMA_BASE_URL

    def test_get_model_override_and_defaults(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_MODEL", raising=False)
        assert llm.get_model("openai") == llm.DEFAULT_OPENAI_MODEL
        assert llm.get_model("ollama") == llm.DEFAULT_OLLAMA_MODEL
        monkeypatch.setenv("CASEPILOT_LLM_MODEL", "qwen2.5:14b")
        assert llm.get_model("ollama") == "qwen2.5:14b"


class TestProbeOllama:
    def test_reachable(self, monkeypatch):
        monkeypatch.setattr(llm.httpx, "get", lambda *a, **k: FakeResponse({"data": []}))
        assert llm.probe_ollama() is True

    def test_server_error_means_unreachable(self, monkeypatch):
        monkeypatch.setattr(llm.httpx, "get", lambda *a, **k: FakeResponse({}, status_code=500))
        assert llm.probe_ollama() is False

    def test_exception_means_unreachable(self, monkeypatch):
        def _boom(*a, **k):
            raise ConnectionRefusedError("连接被拒绝")

        monkeypatch.setattr(llm.httpx, "get", _boom)
        assert llm.probe_ollama() is False

    def test_timeout_is_half_second(self, monkeypatch):
        captured = {}

        def _get(url, timeout=None):
            captured["timeout"] = timeout
            return FakeResponse({})

        monkeypatch.setattr(llm.httpx, "get", _get)
        llm.probe_ollama()
        assert captured["timeout"] == llm.OLLAMA_PROBE_TIMEOUT == 0.5


class TestResolveMode:
    def test_with_key_is_openai(self, monkeypatch):
        monkeypatch.setenv("CASEPILOT_LLM_API_KEY", "k")
        monkeypatch.setattr(
            "casepilot.core.llm.probe_ollama",
            lambda: pytest.fail("有 key 时不应探测 Ollama"),
        )
        assert llm.resolve_mode() == "openai"

    def test_without_key_reachable_is_ollama(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("casepilot.core.llm.probe_ollama", lambda: True)
        assert llm.resolve_mode() == "ollama"

    def test_without_key_unreachable_is_mock(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("casepilot.core.llm.probe_ollama", lambda: False)
        assert llm.resolve_mode() == "mock"


class TestCallChatCompletions:
    def _capture_post(self, monkeypatch, payload):
        captured = {}

        def _post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            captured["timeout"] = timeout
            return FakeResponse(payload)

        monkeypatch.setattr(llm.httpx, "post", _post)
        return captured

    def test_request_shape_openai(self, monkeypatch):
        payload = {"choices": [{"message": {"content": "[]"}}]}
        captured = self._capture_post(monkeypatch, payload)
        llm.call_chat_completions("https://api.example.com/v1", "secret-key", "gpt-x", "系统提示", "用户提示")
        assert captured["url"] == "https://api.example.com/v1/chat/completions"
        assert captured["headers"]["Authorization"] == "Bearer secret-key"
        assert captured["json"]["temperature"] == 0
        assert captured["json"]["messages"][0] == {"role": "system", "content": "系统提示"}
        assert captured["json"]["messages"][1] == {"role": "user", "content": "用户提示"}
        assert captured["json"]["model"] == "gpt-x"

    def test_no_auth_header_without_key(self, monkeypatch):
        payload = {"choices": [{"message": {"content": "[]"}}]}
        captured = self._capture_post(monkeypatch, payload)
        llm.call_chat_completions("http://localhost:11434/v1", None, "qwen", "s", "u")
        assert "Authorization" not in captured["headers"]

    def test_bad_response_structure_raises(self, monkeypatch):
        self._capture_post(monkeypatch, {"choices": []})
        with pytest.raises(ValueError) as exc:
            llm.call_chat_completions("https://x/v1", None, "m", "s", "u")
        assert "OpenAI 兼容协议" in str(exc.value)


class TestParseCasesJson:
    def test_plain_array(self):
        assert llm.parse_cases_json('[{"title": "a"}]') == [{"title": "a"}]

    def test_fenced_array(self):
        content = '```json\n[{"title": "a"}]\n```'
        assert llm.parse_cases_json(content) == [{"title": "a"}]

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError) as exc:
            llm.parse_cases_json("这不是 JSON")
        assert "不是合法 JSON" in str(exc.value)

    def test_non_array_raises(self):
        with pytest.raises(ValueError) as exc:
            llm.parse_cases_json('{"title": "a"}')
        assert "JSON 数组" in str(exc.value)


class TestLLMGateway:
    def test_mock_mode_uses_rule_engine(self):
        gateway = LLMGateway(mode="mock")
        raw = gateway.generate_raw_cases(_profile())
        assert len(raw) >= 8
        assert "mock" in gateway.mode_description

    def test_openai_mode_parses_llm_output(self, monkeypatch):
        monkeypatch.setattr(
            "casepilot.core.llm.call_chat_completions",
            lambda *a, **k: json.dumps([_valid_raw_case()], ensure_ascii=False),
        )
        gateway = LLMGateway(mode="openai")
        raw = gateway.generate_raw_cases(_profile())
        assert raw == [_valid_raw_case()]
        assert "OpenAI" in gateway.mode_description

    def test_ollama_mode_description(self):
        assert "本地 Ollama" in LLMGateway(mode="ollama").mode_description

    def test_default_mode_resolution(self, monkeypatch):
        monkeypatch.setattr("casepilot.core.llm.resolve_mode", lambda: "mock")
        assert LLMGateway().mode == "mock"


class TestGetMaxRetries:
    def test_default_is_two(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_MAX_RETRIES", raising=False)
        assert llm.get_max_retries() == llm.DEFAULT_MAX_RETRIES == 2

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CASEPILOT_LLM_MAX_RETRIES", "5")
        assert llm.get_max_retries() == 5

    def test_invalid_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("CASEPILOT_LLM_MAX_RETRIES", "abc")
        assert llm.get_max_retries() == 2

    def test_negative_clamped_to_zero(self, monkeypatch):
        monkeypatch.setenv("CASEPILOT_LLM_MAX_RETRIES", "-1")
        assert llm.get_max_retries() == 0


class TestAutoRetry:
    """质量门禁：LLM 输出解析 / 校验失败自动重试 ≤2 次（UP-3）。"""

    def _run_with_responses(self, monkeypatch, responses):
        """按序返回替身响应，记录每次调用。"""
        calls = {"count": 0, "prompts": []}

        def _fake(base_url, api_key, model, system, user):
            calls["prompts"].append(user)
            content = responses[calls["count"]] if calls["count"] < len(responses) else responses[-1]
            calls["count"] += 1
            return content

        monkeypatch.setattr("casepilot.core.llm.call_chat_completions", _fake)
        return calls

    def test_bad_json_then_success_retries_once(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_MAX_RETRIES", raising=False)
        calls = self._run_with_responses(
            monkeypatch,
            ["这不是 JSON", json.dumps([_valid_raw_case()], ensure_ascii=False)],
        )
        gateway = LLMGateway(mode="openai")
        raw = gateway.generate_raw_cases(_profile())
        assert calls["count"] == 2, "坏 JSON 后应恰好重试 1 次"
        assert raw == [_valid_raw_case()]
        # 错误信息回灌提示词
        assert "自动重试" in calls["prompts"][1]
        assert "存在问题" in calls["prompts"][1]

    def test_retry_feeds_error_message_back(self, monkeypatch):
        calls = self._run_with_responses(
            monkeypatch,
            ["坏输出", json.dumps([_valid_raw_case()], ensure_ascii=False)],
        )
        LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert "不是合法 JSON" in calls["prompts"][1]  # 具体错误信息回灌

    def test_validation_failure_also_retried(self, monkeypatch):
        bad_but_json = json.dumps([{"title": "缺字段"}], ensure_ascii=False)
        calls = self._run_with_responses(
            monkeypatch,
            [bad_but_json, json.dumps([_valid_raw_case()], ensure_ascii=False)],
        )
        gateway = LLMGateway(mode="openai")
        raw = gateway.generate_raw_cases(_profile())
        assert calls["count"] == 2, "结构校验失败同样应触发重试"
        assert raw == [_valid_raw_case()]

    def test_exhausted_retries_raises_chinese_error(self, monkeypatch):
        monkeypatch.delenv("CASEPILOT_LLM_MAX_RETRIES", raising=False)
        calls = self._run_with_responses(monkeypatch, ["永远是坏 JSON"])
        with pytest.raises(ValueError) as exc:
            LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 3, "默认应共调用 3 次（1 次 + 2 次重试）"
        message = str(exc.value)
        assert "已自动重试 2 次" in message
        assert "最后一次错误" in message

    def test_max_retries_env_controls_attempts(self, monkeypatch):
        monkeypatch.setenv("CASEPILOT_LLM_MAX_RETRIES", "0")
        calls = self._run_with_responses(monkeypatch, ["坏 JSON"])
        with pytest.raises(ValueError) as exc:
            LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 1, "重试次数为 0 时不应重试"
        assert "已自动重试 0 次" in str(exc.value)

    def test_non_array_output_triggers_retry(self, monkeypatch):
        calls = self._run_with_responses(
            monkeypatch,
            ['{"title": "a"}', json.dumps([_valid_raw_case()], ensure_ascii=False)],
        )
        LLMGateway(mode="openai").generate_raw_cases(_profile())
        assert calls["count"] == 2
        assert "JSON 数组" in calls["prompts"][1]
