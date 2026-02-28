"""Multi-provider LLM registry.

Supports: DeepSeek, Qwen (通义千问), OpenAI (ChatGPT), Claude (Anthropic),
Zhipu (智谱/GLM), MiniMax, Doubao (豆包/火山引擎).

Provider selection priority:
1. Explicit ``LLM_PROVIDER`` env var (or per-module override).
2. Auto-detect from first available provider-specific API key.
3. Fall back to legacy ``OPENAI_API_KEY`` + ``OPENAI_BASE_URL``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

from config import build_url_opener

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------

PROVIDER_CATALOG: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "api_format": "openai",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "label": "通义千问 (Qwen)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "api_format": "openai",
        "env_key": "QWEN_API_KEY",
    },
    "openai": {
        "label": "OpenAI (ChatGPT)",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "api_format": "openai",
        "env_key": "OPENAI_API_KEY",
    },
    "claude": {
        "label": "Claude (Anthropic)",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-20250514",
        "api_format": "anthropic",
        "env_key": "CLAUDE_API_KEY",
    },
    "zhipu": {
        "label": "智谱 (Zhipu/GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "api_format": "openai",
        "env_key": "ZHIPU_API_KEY",
    },
    "minimax": {
        "label": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "default_model": "MiniMax-M2.5",
        "api_format": "openai",
        "env_key": "MINIMAX_API_KEY",
    },
    "doubao": {
        "label": "豆包 (Doubao)",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "",
        "api_format": "openai",
        "env_key": "DOUBAO_API_KEY",
        "note": "Model 字段需填火山引擎推理接入点 ID (ep-xxx)",
    },
}

# Order for auto-detection when LLM_PROVIDER is unset.
_DETECT_ORDER = ["deepseek", "qwen", "claude", "zhipu", "minimax", "doubao", "openai"]


def _detect_from_base_url(base_url: str) -> str:
    """Infer provider name from a legacy OPENAI_BASE_URL value."""
    url = (base_url or "").strip().lower()
    if "deepseek" in url:
        return "deepseek"
    if "dashscope" in url or "aliyun" in url:
        return "qwen"
    if "minimax" in url:
        return "minimax"
    if "bigmodel" in url or "zhipu" in url:
        return "zhipu"
    if "volces" in url or "volcengine" in url:
        return "doubao"
    if "anthropic" in url:
        return "claude"
    return "openai"


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def resolve_provider(scope: str = "") -> Tuple[str, str, str, str, str]:
    """Resolve the active LLM provider configuration.

    *scope* can be ``"analyze"`` or ``"recommend"`` to respect per-module
    overrides (``ANALYZE_LLM_PROVIDER``, ``RECOMMEND_LLM_PROVIDER``).

    Returns ``(provider_name, api_key, base_url, model, api_format)``.
    """
    # 1. Check per-module override, then global LLM_PROVIDER.
    name = ""
    if scope:
        name = os.getenv(f"{scope.upper()}_LLM_PROVIDER", "").strip().lower()
    if not name:
        name = os.getenv("LLM_PROVIDER", "").strip().lower()

    # 2. If explicit name given, resolve it.
    if name and name in PROVIDER_CATALOG:
        return _resolve_named(name)

    # 3. Auto-detect from provider-specific API keys.
    for pname in _DETECT_ORDER:
        pdef = PROVIDER_CATALOG[pname]
        if pname == "openai":
            # OpenAI key is checked as legacy fallback (step 4).
            continue
        key = os.getenv(pdef["env_key"], "").strip()
        if key:
            return _resolve_named(pname)

    # 4. Legacy fallback: OPENAI_API_KEY + OPENAI_BASE_URL.
    legacy_key = os.getenv("OPENAI_API_KEY", "").strip()
    if legacy_key:
        legacy_url = os.getenv("OPENAI_BASE_URL", "").strip()
        detected = _detect_from_base_url(legacy_url)
        pdef = PROVIDER_CATALOG.get(detected, PROVIDER_CATALOG["openai"])
        base_url = legacy_url.rstrip("/") if legacy_url else pdef["base_url"]
        model = (
            os.getenv("OPENAI_API_MODEL", "").strip()
            or os.getenv("OPENAI_MODEL", "").strip()
            or pdef["default_model"]
        )
        return (detected, legacy_key, base_url, model, pdef["api_format"])

    return ("none", "", "", "", "none")


def _resolve_named(name: str) -> Tuple[str, str, str, str, str]:
    """Resolve config for a named provider."""
    pdef = PROVIDER_CATALOG[name]
    upper = name.upper()

    # API key: provider-specific first, then OPENAI_API_KEY for openai-compat.
    api_key = os.getenv(pdef["env_key"], "").strip()
    if not api_key and pdef["api_format"] == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()

    # Base URL: provider-specific override, then catalog default.
    base_url = os.getenv(f"{upper}_BASE_URL", "").strip() or pdef["base_url"]

    # Model: provider-specific override, then legacy OPENAI_MODEL, then default.
    model = os.getenv(f"{upper}_MODEL", "").strip()
    if not model and pdef["api_format"] == "openai":
        model = os.getenv("OPENAI_API_MODEL", "").strip() or os.getenv("OPENAI_MODEL", "").strip()
    if not model:
        model = pdef["default_model"]

    return (name, api_key, base_url.rstrip("/"), model, pdef["api_format"])


def list_providers() -> List[Dict[str, Any]]:
    """Return provider catalogue with availability status."""
    result = []
    for name, pdef in PROVIDER_CATALOG.items():
        _, api_key, base_url, model, api_format = _resolve_named(name)
        result.append(
            {
                "id": name,
                "label": pdef["label"],
                "base_url": base_url,
                "model": model,
                "api_format": api_format,
                "configured": bool(api_key),
                "note": pdef.get("note", ""),
            }
        )
    return result


def provider_available(scope: str = "") -> bool:
    """Return True if at least one provider has an API key configured."""
    _, api_key, *_ = resolve_provider(scope)
    return bool(api_key)


# ---------------------------------------------------------------------------
# Claude (Anthropic) format helpers
# ---------------------------------------------------------------------------


def openai_to_anthropic_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an OpenAI-format chat completion payload to Anthropic Messages format."""
    messages = payload.get("messages", [])
    system_parts: List[str] = []
    anthropic_messages: List[Dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            anthropic_messages.append({"role": role, "content": content})

    result: Dict[str, Any] = {
        "model": payload.get("model", ""),
        "max_tokens": payload.get("max_tokens", 1024),
        "messages": anthropic_messages,
    }
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    if "temperature" in payload:
        result["temperature"] = payload["temperature"]
    return result


def anthropic_response_to_openai(response: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an Anthropic Messages response to OpenAI chat completion format."""
    content_blocks = response.get("content", [])
    text_parts: List[str] = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(block.get("text", ""))
    content_text = "\n".join(text_parts)

    usage = response.get("usage", {})
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))

    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content_text},
                "finish_reason": response.get("stop_reason", "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Unified HTTP helpers
# ---------------------------------------------------------------------------


def _do_openai_request(
    api_key: str,
    base_url: str,
    payload: Dict[str, Any],
    timeout: int,
) -> Dict[str, Any]:
    url = f"{base_url}/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    opener = build_url_opener(url)
    try:
        with opener.open(req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"LLM request failed ({exc.code}): {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc
    try:
        return json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"LLM response parse failed: {exc}") from exc


def _do_anthropic_request(
    api_key: str,
    base_url: str,
    payload: Dict[str, Any],
    timeout: int,
) -> Dict[str, Any]:
    claude_payload = openai_to_anthropic_payload(payload)
    url = f"{base_url}/v1/messages"
    body = json.dumps(claude_payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    opener = build_url_opener(url)
    try:
        with opener.open(req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"Claude request failed ({exc.code}): {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"Claude request failed: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"Claude response parse failed: {exc}") from exc
    return anthropic_response_to_openai(parsed)


def post_chat_completion(
    payload: Dict[str, Any],
    scope: str = "",
    timeout: int = 25,
) -> Tuple[Dict[str, Any], str]:
    """Send a chat completion request using the resolved provider.

    Returns ``(openai_format_response, provider_name)``.
    The response is always normalized to OpenAI format regardless of the
    underlying provider.
    """
    name, api_key, base_url, model, api_format = resolve_provider(scope)
    if not api_key:
        raise RuntimeError(
            "No LLM API key configured. "
            "Set LLM_PROVIDER and the corresponding API key environment variable."
        )

    request_payload = dict(payload)
    if not str(request_payload.get("model") or "").strip():
        request_payload["model"] = model

    if api_format == "anthropic":
        return _do_anthropic_request(api_key, base_url, request_payload, timeout), name
    return _do_openai_request(api_key, base_url, request_payload, timeout), name


# ---------------------------------------------------------------------------
# Quick connectivity test
# ---------------------------------------------------------------------------


def test_provider(provider_name: str, timeout: int = 15) -> Dict[str, Any]:
    """Test a provider with a simple request. Returns status dict."""
    if provider_name not in PROVIDER_CATALOG:
        return {"ok": False, "error": f"Unknown provider: {provider_name}", "latency_ms": 0}

    _, api_key, base_url, model, api_format = _resolve_named(provider_name)
    if not api_key:
        return {"ok": False, "error": "API key not configured", "latency_ms": 0}

    if not model:
        return {"ok": False, "error": "Model not configured", "latency_ms": 0}

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi, reply with just 'ok'."}],
        "max_tokens": 8,
        "temperature": 0,
    }
    started = time.perf_counter()
    try:
        if api_format == "anthropic":
            result = _do_anthropic_request(api_key, base_url, payload, timeout)
        else:
            result = _do_openai_request(api_key, base_url, payload, timeout)
        latency_ms = int((time.perf_counter() - started) * 1000)
        content = ""
        choices = result.get("choices", [])
        if choices:
            content = (choices[0].get("message") or {}).get("content", "")
        return {"ok": True, "latency_ms": latency_ms, "response": content[:100]}
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {"ok": False, "error": str(exc)[:300], "latency_ms": latency_ms}
