from __future__ import annotations

from .llm_settings import LLMSettings, load_llm_settings
from .ollama_client import OllamaClient
from .openai_compatible_client import OpenAICompatibleClient


def create_llm_client(
    *,
    model: str | None = None,
    settings: LLMSettings | None = None,
) -> OllamaClient | OpenAICompatibleClient:
    settings = settings or load_llm_settings()
    selected_model = model or settings.model
    if settings.provider == "ollama":
        return OllamaClient(model=selected_model, host=settings.ollama_host, timeout=settings.timeout)
    return OpenAICompatibleClient(
        model=selected_model,
        api_url=settings.api_url,
        api_key=settings.api_key,
        timeout=settings.timeout,
        json_mode=settings.api_json_mode,
        thinking=settings.api_thinking,
    )
