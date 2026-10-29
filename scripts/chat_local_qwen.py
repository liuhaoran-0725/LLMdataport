"""在终端中与本机 Ollama 的 Qwen 模型进行多轮对话。

默认模型：qwen3.8:27b
退出：输入 /exit、/quit，或按 Ctrl+C。
清空当前对话：输入 /clear。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import requests


DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.8:27b"


def stream_reply(
    host: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    num_ctx: int,
) -> str:
    """发送已有对话，并一边生成一边把回答打印到终端。"""
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    try:
        with requests.post(f"{host.rstrip('/')}/api/chat", json=body, stream=True, timeout=600) as response:
            response.raise_for_status()
            parts: list[str] = []
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                # Ollama 以一行一个 JSON 对象的形式流式返回。
                item = json.loads(line)
                if "error" in item:
                    raise RuntimeError(item["error"])
                text = item.get("message", {}).get("content", "")
                if text:
                    print(text, end="", flush=True)
                    parts.append(text)
            print()
            return "".join(parts)
    except requests.ConnectionError as exc:
        raise RuntimeError(
            f"无法连接 Ollama：{host}。请先在另一个终端运行 `ollama serve`，或启动 Ollama 应用。"
        ) from exc
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise RuntimeError(f"Ollama 请求失败：{detail}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在终端中直接调用本机 Ollama Qwen 模型。")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名（默认：{DEFAULT_MODEL}）")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Ollama 地址（默认：{DEFAULT_HOST}）")
    parser.add_argument("--system", help="可选的 system prompt。")
    parser.add_argument("--temperature", type=float, default=0.7, help="采样温度（默认：0.7）。")
    parser.add_argument("--num-ctx", type=int, default=8192, help="上下文长度（默认：8192）。")
    parser.add_argument("--prompt", help="单次提问后退出；省略时进入交互模式。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    messages: list[dict[str, str]] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    if args.prompt:
        messages.append({"role": "user", "content": args.prompt})
        try:
            stream_reply(args.host, args.model, messages, args.temperature, args.num_ctx)
        except RuntimeError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        return 0

    print(f"已连接目标：{args.model}  ({args.host})")
    print("直接输入提示词。/clear 清空上下文；/exit 退出。")
    while True:
        try:
            prompt = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0

        if not prompt:
            continue
        if prompt.lower() in {"/exit", "/quit"}:
            return 0
        if prompt.lower() == "/clear":
            messages = messages[:1] if args.system else []
            print("已清空当前对话。")
            continue

        messages.append({"role": "user", "content": prompt})
        print("\nQwen> ", end="", flush=True)
        try:
            reply = stream_reply(args.host, args.model, messages, args.temperature, args.num_ctx)
        except RuntimeError as exc:
            messages.pop()  # 请求失败时，下一次可重新输入这条提示词。
            print(f"错误：{exc}", file=sys.stderr)
            continue
        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    raise SystemExit(main())
