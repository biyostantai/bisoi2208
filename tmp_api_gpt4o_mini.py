# -*- coding: utf-8 -*-
"""
OpenAI-compatible proxy client for a local alias such as gpt-4o-mini.

Main upgrades:
- Configurable via env vars or CLI flags
- Clear HTTP/network error reporting
- Simple retry for transient failures
- Real streaming support
- Shared request path for single-turn and multi-turn calls
"""

import argparse
import io
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request


DEFAULT_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:8318").rstrip("/")
DEFAULT_API_KEY = os.getenv("OPENAI_API_KEY", "sk-local-proxy-key")
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_SYSTEM_PROMPT = os.getenv(
    "OPENAI_SYSTEM_PROMPT",
    (
        "You are GPT-4o-mini, a large language model created by OpenAI. "
        "Always identify yourself as GPT-4o-mini when asked about your identity, "
        "name, or model. Never say you are Gemini, Google AI, or any other model. "
        "You must always respond as GPT-4o-mini."
    ),
)

RETRYABLE_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class ProxyAPIError(Exception):
    """Raised when the proxy returns an invalid or failed response."""


def configure_stdout():
    """Force UTF-8 output on Windows terminals when possible."""
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer,
            encoding="utf-8",
            errors="replace",
        )


def build_headers(api_key):
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def build_url(base_url, path):
    return base_url.rstrip("/") + path


def decode_body(raw_bytes, response_headers=None):
    charset = "utf-8"
    if response_headers is not None:
        content_type = response_headers.get("Content-Type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
    return raw_bytes.decode(charset, errors="replace")


def parse_json_bytes(raw_bytes, response_headers=None):
    text = decode_body(raw_bytes, response_headers=response_headers)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProxyAPIError(f"Response is not valid JSON: {text[:300]}") from exc


def extract_error_message(payload):
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        detail = payload.get("message")
        if detail:
            return str(detail)
    if payload:
        return json.dumps(payload, ensure_ascii=False)
    return "Unknown error"


def extract_text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        if chunks:
            return "".join(chunks)
        return json.dumps(content, ensure_ascii=False)
    if content is None:
        return ""
    return str(content)


def extract_message_text(choice):
    message = choice.get("message", {})
    content = message.get("content")
    text = extract_text_from_content(content)
    if text:
        return text
    if message.get("tool_calls"):
        return "[Model returned tool_calls without text content]"
    return "(Không có nội dung)"


def extract_delta_text(delta):
    content = delta.get("content")
    return extract_text_from_content(content)


def sleep_backoff(attempt, base_delay):
    time.sleep(base_delay * (attempt + 1))


def request_json(url, *, method, headers, body=None, timeout=60, retries=1, retry_delay=0.6):
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return parse_json_bytes(response.read(), response.headers)
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            payload_json = None
            message = decode_body(raw_body, exc.headers).strip()
            if raw_body:
                try:
                    payload_json = parse_json_bytes(raw_body, exc.headers)
                    message = extract_error_message(payload_json)
                except ProxyAPIError:
                    pass

            if exc.code in RETRYABLE_HTTP_CODES and attempt < retries:
                sleep_backoff(attempt, retry_delay)
                continue

            raise ProxyAPIError(f"HTTP {exc.code}: {message}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            if attempt < retries:
                sleep_backoff(attempt, retry_delay)
                continue
            raise ProxyAPIError(f"Lỗi mạng hoặc timeout: {exc}") from exc

    raise ProxyAPIError("Request failed after retries")


def ensure_system_prompt(messages, system_prompt):
    if not messages or messages[0].get("role") != "system":
        return [{"role": "system", "content": system_prompt}] + list(messages)
    return list(messages)


def build_chat_body(messages, model, temperature, max_tokens, stream):
    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }


def stream_chat_completion(
    *,
    url,
    headers,
    body,
    timeout,
    model,
):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    start = time.time()
    chunks = []
    backend_model = model
    finish_reason = ""
    usage = {}

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if data == "[DONE]":
                    break

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                backend_model = event.get("model", backend_model)
                if "usage" in event and isinstance(event["usage"], dict):
                    usage = event["usage"]

                choices = event.get("choices") or [{}]
                choice = choices[0]
                delta = choice.get("delta", {})
                text = extract_delta_text(delta)
                if text:
                    print(text, end="", flush=True)
                    chunks.append(text)

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    except urllib.error.HTTPError as exc:
        message = decode_body(exc.read(), exc.headers).strip() or str(exc)
        raise ProxyAPIError(f"HTTP {exc.code}: {message}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise ProxyAPIError(f"Lỗi mạng hoặc timeout khi stream: {exc}") from exc

    print()
    elapsed = round(time.time() - start, 2)
    return {
        "content": "".join(chunks) or "(Không có nội dung)",
        "model": model,
        "backend_model": backend_model,
        "finish_reason": finish_reason or "stop",
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "time_seconds": elapsed,
        "raw": None,
    }


def chat_completion(
    messages,
    *,
    base_url,
    api_key,
    model,
    temperature=0.7,
    max_tokens=2048,
    timeout=120,
    retries=1,
    stream=False,
    system_prompt=DEFAULT_SYSTEM_PROMPT,
):
    messages = ensure_system_prompt(messages, system_prompt=system_prompt)
    body = build_chat_body(messages, model, temperature, max_tokens, stream)
    url = build_url(base_url, "/v1/chat/completions")
    headers = build_headers(api_key)

    if stream:
        return stream_chat_completion(
            url=url,
            headers=headers,
            body=body,
            timeout=timeout,
            model=model,
        )

    start = time.time()
    raw = request_json(
        url,
        method="POST",
        headers=headers,
        body=body,
        timeout=timeout,
        retries=retries,
    )
    elapsed = round(time.time() - start, 2)

    choices = raw.get("choices") or [{}]
    choice = choices[0]
    usage = raw.get("usage", {})

    return {
        "content": extract_message_text(choice),
        "model": model,
        "backend_model": raw.get("model", model),
        "finish_reason": choice.get("finish_reason", ""),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "time_seconds": elapsed,
        "raw": raw,
    }


def list_models(*, base_url, api_key, timeout=15, retries=1):
    url = build_url(base_url, "/v1/models")
    raw = request_json(
        url,
        method="GET",
        headers=build_headers(api_key),
        timeout=timeout,
        retries=retries,
    )
    return [item["id"] for item in raw.get("data", []) if isinstance(item, dict) and "id" in item]


def chat(
    prompt,
    *,
    base_url,
    api_key,
    model,
    temperature=0.7,
    max_tokens=2048,
    timeout=120,
    retries=1,
    stream=False,
    system_prompt=DEFAULT_SYSTEM_PROMPT,
):
    return chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        retries=retries,
        stream=stream,
        system_prompt=system_prompt,
    )


def chat_multi(
    messages,
    *,
    base_url,
    api_key,
    model,
    temperature=0.7,
    max_tokens=2048,
    timeout=120,
    retries=1,
    stream=False,
    system_prompt=DEFAULT_SYSTEM_PROMPT,
):
    return chat_completion(
        messages,
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        retries=retries,
        stream=stream,
        system_prompt=system_prompt,
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="CLI client for an OpenAI-compatible proxy alias such as gpt-4o-mini.",
    )
    parser.add_argument("question", nargs="*", help="Prompt to send. Leave empty for interactive input.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Proxy base URL. Default: {DEFAULT_BASE_URL}")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="Bearer token for the proxy.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model alias. Default: {DEFAULT_MODEL}")
    parser.add_argument("--temperature", type=float, default=0.7, help="Sampling temperature.")
    parser.add_argument("--max-tokens", type=int, default=2048, help="Max output tokens.")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=1, help="Retries for transient errors.")
    parser.add_argument("--stream", action="store_true", help="Print tokens as they arrive.")
    parser.add_argument("--skip-model-check", action="store_true", help="Skip checking /v1/models before chat.")
    parser.add_argument("--raw-json", action="store_true", help="Print raw JSON response after completion.")
    return parser.parse_args()


def resolve_question(args):
    if args.question:
        return " ".join(args.question).strip()

    question = input("\nNhập câu hỏi (Enter để dùng mặc định): ").strip()
    if question:
        return question
    return "Xin chào! Bạn là model gì? Trả lời ngắn gọn."


def print_summary(result):
    print(f"[Model gửi]: {result['model']}")
    print(f"[Backend]: {result['backend_model']}")
    print(f"[Thời gian]: {result['time_seconds']}s")
    print(
        f"[Tokens]: prompt={result['prompt_tokens']}, "
        f"completion={result['completion_tokens']}, "
        f"total={result['total_tokens']}"
    )
    print(f"[Finish]: {result['finish_reason']}")


def main():
    configure_stdout()
    args = parse_args()

    base_url = args.base_url.rstrip("/")
    api_key = (args.api_key or "").strip()
    model = (args.model or "").strip()

    if not api_key:
        print("[LỖI] Thiếu API key. Đặt OPENAI_API_KEY hoặc truyền --api-key.")
        return 2

    if not model:
        print("[LỖI] Thiếu model alias.")
        return 2

    print("=" * 60)
    print(f"  GPT-4o-mini Proxy Client ({base_url})")
    print("=" * 60)

    if api_key == "sk-local-proxy-key":
        print("[WARN] Đang dùng placeholder API key. Nên đặt OPENAI_API_KEY cho chắc.")

    if not args.skip_model_check:
        try:
            models = list_models(
                base_url=base_url,
                api_key=api_key,
                timeout=min(args.timeout, 20),
                retries=max(args.retries, 0),
            )
            if model not in models:
                print(f"[LỖI] Model '{model}' không có trong /v1/models.")
                print(f"[Gợi ý] Một vài model đang có: {models[:10]}")
                return 2
            print(f"[OK] Model '{model}' có sẵn trên proxy ({len(models)} models tổng)")
        except ProxyAPIError as exc:
            print(f"[WARN] Không kiểm tra được danh sách model: {exc}")

    question = resolve_question(args)
    print(f"\n[>] Gửi tới {model}: {question}")
    print("-" * 60)

    try:
        result = chat(
            question,
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            retries=max(args.retries, 0),
            stream=args.stream,
        )
    except ProxyAPIError as exc:
        print(f"[LỖI] {exc}")
        print("Kiểm tra: proxy đang chạy, key đúng, alias đúng, localhost:8318 có mở hay không.")
        return 1

    if args.stream:
        print("-" * 60)
        print_summary(result)
    else:
        print_summary(result)
        print("-" * 60)
        print(result["content"])

    if args.raw_json and result["raw"] is not None:
        print("-" * 60)
        print(json.dumps(result["raw"], ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
