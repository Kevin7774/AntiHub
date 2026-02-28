from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from config import GITCODE_API_BASE_URL, GITCODE_SEARCH_PATH, GITCODE_TOKEN, build_url_opener
from runtime_metrics import record_counter_metric, record_timing_metric


class GitCodeAPIError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _normalize_path(path: str) -> str:
    cleaned = str(path or "").strip()
    if not cleaned:
        return "/api/v4/projects"
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return cleaned


def _request_json(url: str, token: Optional[str], timeout: int = 8) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "AntiHub/0.5",
    }
    if token:
        # GitLab-compatible instances usually accept PRIVATE-TOKEN.
        headers["PRIVATE-TOKEN"] = token
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = build_url_opener(url)
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        record_counter_metric(name="recommend.provider.gitcode.http_error", value=1)
        raise GitCodeAPIError("GITCODE_HTTP_ERROR", f"{exc.code} {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        record_counter_metric(name="recommend.provider.gitcode.request_failed", value=1)
        raise GitCodeAPIError("GITCODE_REQUEST_FAILED", str(exc)) from exc
    try:
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        snippet = (raw or "").strip().replace("\n", " ")[:160]
        if snippet.startswith("<!DOCTYPE html") or snippet.startswith("<html"):
            raise GitCodeAPIError(
                "GITCODE_NON_JSON_RESPONSE",
                "GitCode endpoint returned HTML (check API path/token/WAF), expected JSON.",
            ) from exc
        raise GitCodeAPIError("GITCODE_PARSE_FAILED", str(exc)) from exc
    record_timing_metric(name="recommend.provider.gitcode.latency_ms", duration_ms=int((time.perf_counter() - started) * 1000))
    return parsed


def search_repositories(
    query: str,
    per_page: int = 30,
    page: int = 1,
    timeout: int = 8,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not query:
        return [], {}

    base = str(GITCODE_API_BASE_URL or "https://gitcode.com").strip().rstrip("/")
    search_path = _normalize_path(GITCODE_SEARCH_PATH or "/api/v4/projects")
    params = {
        "search": query,
        "order_by": "star_count",
        "sort": "desc",
        "per_page": max(1, min(int(per_page), 100)),
        "page": max(1, int(page)),
        "simple": "true",
    }
    token = str(GITCODE_TOKEN or "").strip() or None
    encoded = urllib.parse.urlencode(params)
    url = f"{base}{search_path}?{encoded}"
    payload = _request_json(url, token, timeout=timeout)

    items: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    if isinstance(payload, list):
        items = [dict(item) for item in payload if isinstance(item, dict)]
        meta = {"total_count": len(items)}
    elif isinstance(payload, dict):
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raw_items = payload.get("data")
        if not isinstance(raw_items, list):
            raw_items = payload.get("projects")
        if isinstance(raw_items, list):
            items = [dict(item) for item in raw_items if isinstance(item, dict)]
        meta = dict(payload)
    return items, meta
