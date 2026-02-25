import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from runtime_metrics import record_counter_metric, record_timing_metric

class GitHubAPIError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _token() -> Optional[str]:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_TOKEN")
    token = (token or "").strip()
    return token or None


def _trim_search_query(query: str, max_encoded_chars: int = 220) -> str:
    """
    Keep GitHub Search API `q` within a safe length budget.

    GitHub rejects oversized search expressions (q > 256 chars) with 422.
    Some upstream gateways may surface this as 414.
    """

    normalized = " ".join(str(query or "").split())
    if len(urllib.parse.quote(normalized)) <= max_encoded_chars:
        return normalized

    suffix = ""
    searchable_suffix = " in:name,description,readme"
    if normalized.endswith(searchable_suffix):
        suffix = searchable_suffix
        normalized = normalized[: -len(searchable_suffix)].strip()

    suffix_encoded = len(urllib.parse.quote(suffix)) if suffix else 0
    budget = max(1, max_encoded_chars - suffix_encoded)
    tokens = [token for token in normalized.split(" ") if token]
    kept: list[str] = []
    for token in tokens:
        candidate = token if not kept else f"{' '.join(kept)} {token}"
        if len(urllib.parse.quote(candidate)) > budget:
            break
        kept.append(token)

    trimmed = " ".join(kept).strip()
    if not trimmed:
        # Fallback for very long no-space tokens.
        probe = normalized
        while probe and len(urllib.parse.quote(probe)) > budget:
            probe = probe[:-1]
        trimmed = probe.strip()

    if suffix and len(urllib.parse.quote(f"{trimmed}{suffix}")) <= max_encoded_chars:
        trimmed = f"{trimmed}{suffix}"
    if not trimmed:
        probe = normalized
        while probe and len(urllib.parse.quote(probe)) > max_encoded_chars:
            probe = probe[:-1]
        trimmed = probe.strip()
    return trimmed


def _request_json(url: str, token: Optional[str], timeout: int = 12) -> Dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "AntiHub/0.5",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        record_counter_metric(name="recommend.provider.github.http_error", value=1)
        if exc.code == 403 and "rate limit" in detail.lower():
            raise GitHubAPIError("GITHUB_RATE_LIMIT", detail) from exc
        raise GitHubAPIError("GITHUB_HTTP_ERROR", f"{exc.code} {detail}") from exc
    except Exception as exc:
        record_counter_metric(name="recommend.provider.github.request_failed", value=1)
        raise GitHubAPIError("GITHUB_REQUEST_FAILED", str(exc)) from exc
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise GitHubAPIError("GITHUB_PARSE_FAILED", str(exc)) from exc
    if not isinstance(parsed, dict):
        raise GitHubAPIError("GITHUB_PARSE_FAILED", "response payload is not an object")
    record_timing_metric(name="recommend.provider.github.latency_ms", duration_ms=int((time.perf_counter() - started) * 1000))
    return dict(parsed)


def search_repositories(
    query: str,
    per_page: int = 30,
    page: int = 1,
    timeout: int = 12,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not query:
        return [], {}
    token = _token()
    safe_query = _trim_search_query(query)
    encoded = urllib.parse.quote(safe_query)
    url = (
        "https://api.github.com/search/repositories"
        f"?q={encoded}&sort=stars&order=desc&per_page={per_page}&page={page}"
    )
    payload = _request_json(url, token, timeout=timeout)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return [], payload if isinstance(payload, dict) else {}
    return items, payload


def fetch_repo(full_name: str, timeout: int = 12) -> Dict[str, Any]:
    if not full_name:
        return {}
    token = _token()
    url = f"https://api.github.com/repos/{full_name}"
    payload = _request_json(url, token, timeout=timeout)
    return payload if isinstance(payload, dict) else {}
