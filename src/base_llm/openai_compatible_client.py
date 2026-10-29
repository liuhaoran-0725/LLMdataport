from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

import requests

from .json_utils import parse_json_object


class OpenAICompatibleClient:
    """Client for vision-capable chat-completions APIs using OpenAI's message format."""

    def __init__(
        self,
        *,
        model: str,
        api_url: str,
        api_key: str,
        timeout: int = 600,
        json_mode: bool = False,
        thinking: str = "",
    ) -> None:
        self.model = model
        self.api_url = api_url.strip()
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.json_mode = json_mode
        self.thinking = thinking.strip()

        missing = [
            name
            for name, value in (
                ("API URL", self.api_url),
                ("API key", self.api_key),
                ("model name", self.model),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "OpenAI-compatible model is selected, but the following settings are empty: "
                + ", ".join(missing)
                + ". Configure src/base_llm/provider_config_local.py or the matching environment variables."
            )

    @staticmethod
    def image_to_data_url(image_path: Path) -> str:
        mime_type, _ = mimetypes.guess_type(image_path.name)
        if not mime_type or not mime_type.startswith("image/"):
            mime_type = "image/png"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
            )
        return ""

    def chat_json(
        self,
        prompt: str,
        *,
        images: list[Path] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        num_ctx: int = 8192,
        num_predict: int = 4096,
        raw_output_path: Path | None = None,
    ) -> dict[str, Any]:
        del num_ctx  # Context size is selected by the API model, not by chat-completions requests.
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})

        if images:
            user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image_path in images:
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": self.image_to_data_url(image_path)},
                    }
                )
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_completion_tokens": num_predict,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.thinking:
            body["thinking"] = {"type": self.thinking}

        response = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        if not response.ok:
            detail = response.text[:2000]
            raise RuntimeError(
                f"OpenAI-compatible API returned HTTP {response.status_code}: {detail}"
            )

        payload = response.json()
        try:
            content = self._content_text(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("The API response did not contain choices[0].message.content.") from exc
        if not content.strip():
            raise ValueError("The API returned an empty message content.")

        if raw_output_path:
            raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            raw_output_path.write_text(content, encoding="utf-8")
        return parse_json_object(content)
