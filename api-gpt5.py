# -*- coding: utf-8 -*-
"""
API gọi model GPT-5 qua proxy OpenAI-compatible (localhost:8318).
Tối ưu phản hồi nhanh: token thấp, nhiệt thấp, có tùy chọn gọi song song.

Cách chạy:
  python api-gpt5.py
  python api-gpt5.py "Câu hỏi của bạn"
  python api-gpt5.py "Câu hỏi của bạn" -n 3
"""

import argparse
import io
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_URL = "http://localhost:8318"
API_KEY = "sk-my-secret-key-123"
MODEL = "gpt-5"

CHAT_URL = BASE_URL + "/v1/chat/completions"
MODELS_URL = BASE_URL + "/v1/models"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer " + API_KEY,
}

FAST_SYSTEM = (
    "CRITICAL: Trả lời nhanh, chỉ output nội dung cuối cùng. "
    "Không hiển thị thinking/chain-of-thought. Không markdown."
)


def list_models():
    req = urllib.request.Request(MODELS_URL, headers=HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=12) as res:
        data = json.loads(res.read().decode("utf-8"))
    return [m.get("id", "") for m in data.get("data", [])]


def chat(prompt, model=MODEL, temperature=0.2, max_tokens=320, timeout=45):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": FAST_SYSTEM},
            {"role": "user", "content": prompt},
        ],
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
    start = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = json.loads(res.read().decode("utf-8"))
    elapsed = round(time.time() - start, 3)
    choice = raw.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content", "(Không có nội dung)")
    usage = raw.get("usage", {})
    return {
        "content": content,
        "model": raw.get("model", model),
        "finish_reason": choice.get("finish_reason", ""),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "time_seconds": elapsed,
        "raw": raw,
    }


def fastest_of_parallel(prompt, n=2, **kwargs):
    n = max(1, int(n))
    if n == 1:
        return chat(prompt, **kwargs)

    futures = []
    errors = []
    pool = ThreadPoolExecutor(max_workers=n)
    try:
        for _ in range(n):
            futures.append(pool.submit(chat, prompt, **kwargs))
        for fut in as_completed(futures):
            try:
                result = fut.result()
                for other in futures:
                    if other is not fut:
                        other.cancel()
                return result
            except Exception as e:
                errors.append(str(e))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    raise RuntimeError("Tất cả request song song đều lỗi: " + " | ".join(errors[:3]))


def _parse_args():
    parser = argparse.ArgumentParser(description="GPT-5 fast API tester")
    parser.add_argument("question", nargs="*", help="Câu hỏi gửi đến model")
    parser.add_argument("-n", "--parallel", type=int, default=1, help="Số request chạy song song (1-3)")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=320)
    parser.add_argument("--timeout", type=int, default=45)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    print("=" * 50)
    print(f"  GPT-5 API Fast Test (proxy {BASE_URL})")
    print("=" * 50)

    try:
        models = list_models()
        if MODEL in models:
            print(f"[OK] Model '{MODEL}' có sẵn ({len(models)} models)")
        else:
            print(f"[WARN] Model '{MODEL}' không thấy. Ví dụ models: {models[:8]}")
    except Exception as e:
        print(f"[WARN] Không lấy được danh sách model: {e}")

    if args.question:
        question = " ".join(args.question).strip()
    else:
        question = input("\nNhập câu hỏi (Enter để dùng mặc định): ").strip()
        if not question:
            question = "Xin chào! Bạn là model gì? Trả lời ngắn gọn."

    parallel = min(max(args.parallel, 1), 3)
    print(f"\n[>] Gửi tới {MODEL} | parallel={parallel}: {question}")
    print("-" * 50)

    try:
        result = fastest_of_parallel(
            question,
            n=parallel,
            model=MODEL,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
        print(f"[Model]: {result['model']}")
        print(f"[Thời gian]: {result['time_seconds']}s")
        print(
            f"[Tokens]: prompt={result['prompt_tokens']}, "
            f"completion={result['completion_tokens']}, total={result['total_tokens']}"
        )
        print(f"[Finish]: {result['finish_reason']}")
        print("-" * 50)
        print(result["content"])
    except Exception as e:
        print(f"[LỖI] {e}")
        print("Kiểm tra: proxy đang chạy? (docker ps / localhost:8318)")
