#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import OPENAI_API_KEY, OPENAI_API_MODEL, OPENAI_BASE_URL


def normalize_base_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "https://api.openai.com/v1"
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}***{secret[-4:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LLM connectivity with current env config.")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    parser.add_argument("--model", default="", help="Override model name")
    parser.add_argument("--base-url", default="", help="Override OPENAI base URL")
    args = parser.parse_args()

    api_key = (OPENAI_API_KEY or "").strip()
    model = (args.model or OPENAI_API_MODEL or "").strip()
    base_url = normalize_base_url(args.base_url or OPENAI_BASE_URL)

    if not api_key:
        print("[llm-self-check] FAILED: OPENAI_API_KEY is missing")
        return 1
    if not model:
        print("[llm-self-check] FAILED: OPENAI_API_MODEL is missing")
        return 1

    endpoint = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return a one-word response."},
            {"role": "user", "content": "ok"},
        ],
        "temperature": 0,
        "max_tokens": 8,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    print(
        f"[llm-self-check] probing endpoint={endpoint} model={model} api_key={mask(api_key)} timeout={args.timeout}s"
    )

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        print(f"[llm-self-check] FAILED: HTTP {exc.code} {detail[:400]}")
        return 2
    except Exception as exc:
        print(f"[llm-self-check] FAILED: request error: {exc}")
        return 2

    try:
        data = json.loads(raw)
    except Exception as exc:
        print(f"[llm-self-check] FAILED: invalid JSON response: {exc}")
        return 3

    choices = data.get("choices") or []
    if not choices:
        print("[llm-self-check] FAILED: no choices in response")
        return 3
    message = choices[0].get("message") or {}
    content = str(message.get("content") or "").strip()
    if not content:
        print("[llm-self-check] FAILED: empty content in response")
        return 3

    print(f"[llm-self-check] OK: received content='{content[:80]}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
