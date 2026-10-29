from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from . import provider_config_local as local_config
except ImportError:
    local_config = None


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    ollama_host: str
    ollama_model: str
    api_url: str
    api_key: str
    api_model: str
    api_json_mode: bool
    api_thinking: str
    timeout: int

    @property
    def model(self) -> str:
        if self.provider == "openai_compatible":
            return self.api_model
        return self.ollama_model


def _value(local_name: str, env_name: str, default: str) -> str:
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return env_value.strip()
    if local_config is not None:
        local_value = getattr(local_config, local_name, None)
        if local_value is not None:
            return str(local_value).strip()
    return default


def _bool_value(local_name: str, env_name: str, default: bool) -> bool:
    raw = _value(local_name, env_name, str(default)).lower()
    return raw in {"1", "true", "yes", "on"}


def load_llm_settings() -> LLMSettings:
    provider = _value("LLM_PROVIDER", "BASE_LLM_PROVIDER", "ollama").lower()
    provider = provider.replace("-", "_")
    if provider == "openai":
        provider = "openai_compatible"
    if provider not in {"ollama", "openai_compatible"}:
        raise ValueError(
            "BASE_LLM_PROVIDER must be 'ollama' or 'openai_compatible'."
        )

    try:
        timeout = int(_value("LLM_TIMEOUT_SECONDS", "BASE_LLM_TIMEOUT_SECONDS", "600"))
    except ValueError as exc:
        raise ValueError("BASE_LLM_TIMEOUT_SECONDS must be an integer.") from exc

    return LLMSettings(
        provider=provider,
        ollama_host=_value("OLLAMA_HOST", "OLLAMA_HOST", "http://localhost:11434").rstrip("/"),
        ollama_model=_value("OLLAMA_MODEL", "OLLAMA_MODEL", "qwen3.8:27b"),
        api_url=_value(
            "OPENAI_COMPATIBLE_CHAT_COMPLETIONS_URL",
            "BASE_LLM_API_URL",
            "",
        ),
        api_key=_value("OPENAI_COMPATIBLE_API_KEY", "BASE_LLM_API_KEY", ""),
        api_model=_value("OPENAI_COMPATIBLE_MODEL", "BASE_LLM_API_MODEL", ""),
        api_json_mode=_bool_value("OPENAI_COMPATIBLE_JSON_MODE", "BASE_LLM_API_JSON_MODE", False),
        api_thinking=_value("OPENAI_COMPATIBLE_THINKING", "BASE_LLM_API_THINKING", ""),
        timeout=timeout,
    )
