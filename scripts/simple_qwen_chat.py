"""最简单的本机 Qwen 调用：运行脚本后输入提示词，再获得回答。"""

import requests


MODEL = "qwen3.8:27b"


def main():
    prompt = input("请输入给 Qwen 的提示词：\n> ").strip()
    if not prompt:
        print("没有输入提示词，已结束。")
        return

    response = requests.post(
        "http://127.0.0.1:11434/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False, "think": False},
        timeout=600,
    )
    response.raise_for_status()
    print("\nQwen 的回答：\n")
    print(response.json()["response"])


if __name__ == "__main__":
    main()
