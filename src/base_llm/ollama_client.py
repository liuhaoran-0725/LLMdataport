from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import requests

from .json_utils import parse_json_object


class OllamaClient:
    def __init__(self, model: str, host: str = "http://localhost:11434", timeout: int = 600) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def image_to_base64(self, image_path: Path) -> str:
        return base64.b64encode(image_path.read_bytes()).decode("ascii")

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
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user_message: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            user_message["images"] = [self.image_to_base64(path) for path in images]
        messages.append(user_message)

        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {
                "temperature": temperature,
                "top_p": 0.9,
                "num_ctx": num_ctx,
                "num_predict": num_predict,
            },
        }
        response = requests.post(f"{self.host}/api/chat", json=body, timeout=self.timeout)
        response.raise_for_status()
        content = response.json()["message"]["content"]
        if raw_output_path:
            raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            raw_output_path.write_text(content, encoding="utf-8")
        return parse_json_object(content)
