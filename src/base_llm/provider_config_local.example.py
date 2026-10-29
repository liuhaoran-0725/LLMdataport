"""Example MiniMax API settings. The active local config is provider_config_local.py."""

LLM_PROVIDER = "openai_compatible"
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen3.8:27b"

OPENAI_COMPATIBLE_CHAT_COMPLETIONS_URL = "https://api.minimax.io/v1/chat/completions"
OPENAI_COMPATIBLE_API_KEY = "paste-your-api-key-here"
OPENAI_COMPATIBLE_MODEL = "MiniMax-M3"
OPENAI_COMPATIBLE_JSON_MODE = False
OPENAI_COMPATIBLE_THINKING = "disabled"
LLM_TIMEOUT_SECONDS = 600
