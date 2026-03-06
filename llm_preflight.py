# -*- coding: utf-8 -*-
"""
Quick LLM connectivity checks for FuBot.

Runs lightweight /models + /chat tests against effective GPT/DeepSeek routes.
Use before starting the bot to catch invalid key/model/base URL early.
"""

import json
import sys
import urllib.error
import urllib.request

import config


def _build_url(base_url: str, path: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith(path):
        return base
    if base.endswith("/v1"):
        return base + path
    return base + "/v1" + path


def _is_openrouter(url: str) -> bool:
    return "openrouter.ai" in str(url or "").lower()


def _headers(api_key: str, url: str) -> dict:
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {str(api_key or '').strip()}",
    }
    if _is_openrouter(url):
        referer = str(getattr(config, "OPENROUTER_HTTP_REFERER", "")).strip()
        title = str(getattr(config, "OPENROUTER_X_TITLE", "")).strip()
        if referer:
            h["HTTP-Referer"] = referer
        if title:
            h["X-Title"] = title
    return h


def _key_hint(key: str) -> str:
    v = str(key or "").strip()
    if not v:
        return "<empty>"
    if len(v) <= 10:
        return f"<set:{len(v)}>"
    return f"{v[:6]}...{v[-4:]} (len={len(v)})"


def _test_models(base_url: str, api_key: str) -> tuple[bool, str]:
    url = _build_url(base_url, "/models")
    req = urllib.request.Request(url, headers=_headers(api_key, url), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        data = payload.get("data", [])
        return True, f"{len(data)} models"
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        return False, f"HTTP {e.code}: {err[:180]}"
    except Exception as e:  # pragma: no cover - best effort CLI
        return False, f"{type(e).__name__}: {e}"


def _test_chat(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    url = _build_url(base_url, "/chat/completions")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply exactly with PONG"}],
        "temperature": 0,
        "max_tokens": 12,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(api_key, url),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        content = (
            payload.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not content:
            return False, "empty content"
        return True, content[:80]
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        return False, f"HTTP {e.code}: {err[:180]}"
    except Exception as e:  # pragma: no cover - best effort CLI
        return False, f"{type(e).__name__}: {e}"


def _route_check(label: str, base_url: str, api_key: str, model: str) -> int:
    print(f"[{label}] base={base_url} model={model} key={_key_hint(api_key)}")
    if not str(api_key or "").strip():
        print(f"[{label}] FAIL: API key is empty")
        return 1
    ok_models, msg_models = _test_models(base_url, api_key)
    print(f"[{label}] /models: {'OK' if ok_models else 'FAIL'} - {msg_models}")
    ok_chat, msg_chat = _test_chat(base_url, api_key, model)
    print(f"[{label}] /chat:   {'OK' if ok_chat else 'FAIL'} - {msg_chat}")
    return 0 if (ok_models and ok_chat) else 1


def main() -> int:
    print("=" * 56)
    print("FuBot LLM preflight")
    print("=" * 56)

    routes = [
        ("GPT", config.GPT_BASE_URL, config.GPT_API_KEY, config.GPT_MODEL),
        (
            "DEEPSEEK",
            config.DEEPSEEK_BASE_URL,
            config.DEEPSEEK_API_KEY,
            config.DEEPSEEK_MODEL,
        ),
    ]
    seen = set()
    failures = 0

    for label, base, key, model in routes:
        sig = (str(base).strip(), str(key).strip(), str(model).strip())
        if sig in seen:
            continue
        seen.add(sig)
        failures += _route_check(label, base, key, model)

    if failures:
        print(f"[PREFLIGHT] FAIL ({failures} route(s))")
        return 1
    print("[PREFLIGHT] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

