# -*- coding: utf-8 -*-
"""
API gọi model qua OpenRouter (OpenAI-compatible).

Cách chạy:
  python api-gpt4o-mini.py
  python api-gpt4o-mini.py "Câu hỏi của bạn"
"""

import urllib.request
import json
import sys
import io
import time
import os
from dotenv import load_dotenv

load_dotenv()

# Fix encoding Windows CMD
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

# ============ CẤU HÌNH ============
BASE_URL = os.getenv("GPT_BASE_URL", "https://openrouter.ai/api/v1")
API_KEY = os.getenv("GPT_API_KEY", "")
MODEL = os.getenv("GPT_MODEL", "deepseek/deepseek-v3.2")
# ===================================

# System prompt mặc định
SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Respond concisely and clearly."
)

def build_url(base_url, path):
    """Build URL an toàn: hỗ trợ base có/không có /v1."""
    url = base_url.rstrip("/")
    if url.endswith(path):
        return url
    if url.endswith("/v1"):
        return url + path.replace("/v1", "", 1)
    return url + path


CHAT_URL = build_url(BASE_URL, "/v1/chat/completions")
MODELS_URL = build_url(BASE_URL, "/v1/models")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer " + API_KEY,
}


def list_models():
    """Lấy danh sách model có sẵn trên proxy."""
    req = urllib.request.Request(MODELS_URL, headers=HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=15) as res:
        data = json.loads(res.read().decode("utf-8"))
    return [m["id"] for m in data.get("data", [])]


def chat(prompt, model=MODEL, temperature=0.7, max_tokens=2048, stream=False):
    """Gửi chat tới proxy, trả về nội dung trả lời."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    req = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=120) as res:
        raw = json.loads(res.read().decode("utf-8"))
    elapsed = round(time.time() - start, 2)

    choice = raw.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content", "(Không có nội dung)")
    usage = raw.get("usage", {})

    return {
        "content": content,
        "model": model,  # Luôn hiện tên model đã gửi, không lấy tên backend
        "backend_model": raw.get("model", model),
        "finish_reason": choice.get("finish_reason", ""),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "time_seconds": elapsed,
        "raw": raw,
    }


def chat_multi(messages, model=MODEL, temperature=0.7, max_tokens=2048):
    """Gửi nhiều tin nhắn (multi-turn conversation)."""
    # Tự thêm system prompt nếu chưa có
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        raw = json.loads(res.read().decode("utf-8"))
    return raw.get("choices", [{}])[0].get("message", {}).get("content", "")


if __name__ == "__main__":
    print("=" * 50)
    print(f"  AI API Test ({BASE_URL})")
    print("=" * 50)

    # Kiểm tra model có sẵn
    try:
        models = list_models()
        if MODEL in models:
            print(f"[OK] Model '{MODEL}' có sẵn trên proxy ({len(models)} models tổng)")
        else:
            print(f"[WARN] Model '{MODEL}' không thấy trong danh sách: {models[:5]}...")
    except Exception as e:
        print(f"[WARN] Không lấy được danh sách model: {e}")

    # Lấy câu hỏi
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
    else:
        question = input("\nNhập câu hỏi (Enter để dùng mặc định): ").strip()
        if not question:
            question = "Xin chào! Bạn là model gì? Trả lời ngắn gọn."

    print(f"\n[>] Gửi tới {MODEL}: {question}")
    print("-" * 50)

    try:
        result = chat(question)
        print(f"[Model gửi]: {result['model']}")
        print(f"[Backend]: {result['backend_model']}")
        print(f"[Thời gian]: {result['time_seconds']}s")
        print(
            f"[Tokens]: prompt={result['prompt_tokens']}, "
            f"completion={result['completion_tokens']}, "
            f"total={result['total_tokens']}"
        )
        print(f"[Finish]: {result['finish_reason']}")
        print("-" * 50)
        print(result["content"])
    except Exception as e:
        print(f"[LỖI] {e}")
        print("Kiểm tra: proxy đang chạy? (docker ps / localhost:8318)")
