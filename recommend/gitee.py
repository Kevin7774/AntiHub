from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from config import GITEE_API_BASE_URL, GITEE_TOKEN, build_url_opener
from runtime_metrics import record_counter_metric, record_timing_metric


class GiteeAPIError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _request_json(url: str, token: Optional[str], timeout: int = 8) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "AntiHub/0.5",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = build_url_opener(url)
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        record_counter_metric(name="recommend.provider.gitee.http_error", value=1)
        raise GiteeAPIError("GITEE_HTTP_ERROR", f"{exc.code} {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        record_counter_metric(name="recommend.provider.gitee.request_failed", value=1)
        raise GiteeAPIError("GITEE_REQUEST_FAILED", str(exc)) from exc
    try:
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise GiteeAPIError("GITEE_PARSE_FAILED", str(exc)) from exc
    record_timing_metric(name="recommend.provider.gitee.latency_ms", duration_ms=int((time.perf_counter() - started) * 1000))
    return parsed


def search_repositories(
    query: str,
    per_page: int = 30,
    page: int = 1,
    timeout: int = 8,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not query:
        return [], {}
    base = str(GITEE_API_BASE_URL or "https://gitee.com/api/v5").strip().rstrip("/")
    params = {
        "q": query,
        "sort": "stars_count",
        "order": "desc",
        "per_page": max(1, min(int(per_page), 100)),
        "page": max(1, int(page)),
    }
    token = str(GITEE_TOKEN or "").strip() or None
    if token:
        params["access_token"] = token
    encoded = urllib.parse.urlencode(params)
    url = f"{base}/search/repositories?{encoded}"
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
            raw_items = payload.get("repositories")
        if isinstance(raw_items, list):
            items = [dict(item) for item in raw_items if isinstance(item, dict)]
        meta = dict(payload)
    return items, meta
