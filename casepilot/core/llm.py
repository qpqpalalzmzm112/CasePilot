"""LLM 网关：双模式自动降级（核心差异化）。

模式决策顺序：
1. 配置了 API key（CASEPILOT_LLM_API_KEY 或 OPENAI_API_KEY）→ openai（OpenAI 兼容协议真实调用）
2. 无 key 时探测本地 Ollama（http://localhost:11434/v1，500ms 超时）→ ollama
3. Ollama 不可达 → mock（确定性规则引擎，纯离线可跑）

密钥只从环境变量读取，禁止硬编码；日志全程中文。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx

from . import mock_engine
from .methodology import build_system_prompt, build_user_prompt
from .schema import DocumentProfile, LLMMode

logger = logging.getLogger("casepilot")

# 默认配置（均可通过环境变量覆盖，密钥只走环境变量）
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OLLAMA_MODEL = "qwen2.5"
OLLAMA_PROBE_TIMEOUT = 0.5  # 秒
HTTP_TIMEOUT = 60.0

# 模式中文描述（降级时给用户的明确提示）
MODE_DESCRIPTIONS: dict[str, str] = {
    "openai": "openai（使用 OpenAI 兼容 API 真实调用）",
    "ollama": "ollama（使用本地 Ollama 模型调用）",
    "mock": "mock（无 API key 且本地 Ollama 不可达，已降级为确定性规则引擎，纯离线生成）",
}


def get_api_key() -> Optional[str]:
    """从环境变量读取 API key（兼容 OPENAI_API_KEY），不做任何硬编码。"""
    return os.environ.get("CASEPILOT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")


def get_base_url(mode: LLMMode) -> str:
    """按模式解析 base_url，可用 CASEPILOT_LLM_BASE_URL 覆盖。"""
    override = os.environ.get("CASEPILOT_LLM_BASE_URL")
    if override:
        return override.rstrip("/")
    if mode == "ollama":
        return DEFAULT_OLLAMA_BASE_URL
    return DEFAULT_OPENAI_BASE_URL


def get_model(mode: LLMMode) -> str:
    """按模式解析模型名，可用 CASEPILOT_LLM_MODEL 覆盖。"""
    override = os.environ.get("CASEPILOT_LLM_MODEL")
    if override:
        return override
    return DEFAULT_OLLAMA_MODEL if mode == "ollama" else DEFAULT_OPENAI_MODEL


def probe_ollama(base_url: str = DEFAULT_OLLAMA_BASE_URL) -> bool:
    """探测本地 Ollama 是否可达（500ms 超时），任何异常均视为不可达。"""
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/models", timeout=OLLAMA_PROBE_TIMEOUT)
        return resp.status_code < 500
    except Exception:  # noqa: BLE001 - 网络不可达 / 超时 / 连接拒绝统一降级
        return False


def decide_mode(has_api_key: bool, ollama_reachable: bool) -> LLMMode:
    """模式决策纯函数（便于单测覆盖）：key > Ollama > mock。"""
    if has_api_key:
        return "openai"
    return "ollama" if ollama_reachable else "mock"


def resolve_mode() -> LLMMode:
    """读取环境变量并探测 Ollama，决定当前 LLM 模式。"""
    has_key = bool(get_api_key())
    if has_key:
        return decide_mode(has_api_key=True, ollama_reachable=False)
    reachable = probe_ollama()
    return decide_mode(has_api_key=False, ollama_reachable=reachable)


def call_chat_completions(
    base_url: str, api_key: Optional[str], model: str, system: str, user: str
) -> str:
    """OpenAI 兼容协议调用（Ollama 的 /v1/chat/completions 同样适用）。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"LLM 响应结构不符合 OpenAI 兼容协议：{exc}") from exc


def parse_cases_json(content: str) -> list[dict]:
    """从 LLM 输出中解析 JSON 数组（容忍 ```json 代码块包裹），解析失败抛清晰错误。"""
    text = content.strip()
    if text.startswith("```"):
        # 去掉 markdown 代码块围栏
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 输出不是合法 JSON，无法解析用例数组：{exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("LLM 输出应为 JSON 数组（用例列表），实际不是数组")
    return parsed


class LLMGateway:
    """LLM 多模型网关：统一入口，自动降级。"""

    def __init__(self, mode: Optional[LLMMode] = None) -> None:
        # mode 未显式指定时自动决策
        self.mode: LLMMode = mode or resolve_mode()

    @property
    def mode_description(self) -> str:
        """当前模式的中文描述，用于 CLI / 日志提示。"""
        return MODE_DESCRIPTIONS[self.mode]

    def generate_raw_cases(self, profile: DocumentProfile) -> list[dict]:
        """生成用例原始 dict 列表；mock 模式走确定性规则引擎。"""
        if self.mode == "mock":
            return mock_engine.generate(profile)

        system = build_system_prompt()
        user = build_user_prompt(profile)
        base_url = get_base_url(self.mode)
        api_key = get_api_key() if self.mode == "openai" else None
        model = get_model(self.mode)
        logger.info("调用 LLM：%s（模型 %s）", self.mode_description, model)
        content = call_chat_completions(base_url, api_key, model, system, user)
        return parse_cases_json(content)
