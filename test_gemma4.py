#!/usr/bin/env python3
"""Quick test for vLLM server running Gemma 4."""

from openai import OpenAI

BASE_URL = "http://10.22.100.103:8000/v1"
MODEL = "google/gemma-4-E4B-it"

client = OpenAI(base_url=BASE_URL, api_key="none")


def chat(prompt: str, **kwargs) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        **kwargs,
    )
    return resp.choices[0].message.content


def main():
    # 1. Basic reply
    print("=== Basic ===")
    print(chat("用繁體中文說：你好，我是 Gemma 4。"))

    # 2. Reasoning
    print("\n=== Reasoning ===")
    print(chat("台北到高雄搭高鐵約幾分鐘？請簡短回答。"))

    # 3. Token usage
    print("\n=== Token usage ===")
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "1+1=?"}],
        max_tokens=32,
    )
    print(f"Answer: {resp.choices[0].message.content}")
    print(f"Usage: prompt={resp.usage.prompt_tokens}, "
          f"completion={resp.usage.completion_tokens}, "
          f"total={resp.usage.total_tokens}")


if __name__ == "__main__":
    main()
