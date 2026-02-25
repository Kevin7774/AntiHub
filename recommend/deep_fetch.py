from __future__ import annotations

import base64
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from config import OPENCLAW_BASE_URL
from ingest.openclaw import OpenClawClient, OpenClawClientError
from runtime_metrics import record_counter_metric, record_timing_metric


def _build_opener_for_url(url: str) -> urllib.request.OpenerDirector:
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.startswith("127."):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _http_get_text(url: str, timeout: int = 10, headers: Optional[Dict[str, str]] = None) -> str:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    opener = _build_opener_for_url(url)
    with opener.open(request, timeout=timeout) as resp:  # nosec B310
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _clean_html_to_text(raw_html: str) -> str:
    payload = str(raw_html or "")
    payload = re.sub(r"(?is)<script.*?>.*?</script>", " ", payload)
    payload = re.sub(r"(?is)<style.*?>.*?</style>", " ", payload)
    payload = re.sub(r"(?is)<[^>]+>", " ", payload)
    payload = html.unescape(payload)
    payload = re.sub(r"\s+", " ", payload).strip()
    return payload


def _trim_doc(text: str, limit: int = 2400) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    return value[:limit]


def _repo_full_name(item: Dict[str, Any]) -> str:
    full_name = str(item.get("full_name") or "").strip()
    if full_name:
        return full_name
    url = str(item.get("html_url") or "").strip().rstrip("/")
    match = re.search(r"(github|gitee|gitcode)\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)$", url)
    if not match:
        return ""
    return f"{match.group('owner')}/{match.group('repo')}"


def _readme_candidates(item: Dict[str, Any]) -> List[str]:
    base = str(item.get("html_url") or "").strip().rstrip("/")
    if not base:
        return []
    host = (urlparse(base).hostname or "").lower()
    full_name = _repo_full_name(item)

    candidates: List[str] = []
    if "github.com" in host and full_name:
        owner, repo = full_name.split("/", 1)
        candidates.extend(
            [
                f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md",
                f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/readme.md",
            ]
        )
    if "gitee.com" in host:
        candidates.extend([f"{base}/raw/master/README.md", f"{base}/raw/main/README.md"])
    if "gitcode.com" in host:
        candidates.extend([f"{base}/-/raw/main/README.md", f"{base}/-/raw/master/README.md"])

    candidates.extend(
        [
            f"{base}/README.md",
            f"{base}/readme.md",
            f"{base}/blob/main/README.md",
            f"{base}/blob/master/README.md",
            base,
        ]
    )
    deduped: List[str] = []
    seen: set[str] = set()
    for url in candidates:
        normalized = str(url or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _fetch_github_readme_via_api(full_name: str, timeout: int) -> Tuple[str, Optional[str], Optional[str]]:
    if not full_name:
        return "", None, None
    token = str(os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_TOKEN") or "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "AntiHub/0.5",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{full_name}/readme"
    try:
        payload_raw = _http_get_text(url, timeout=timeout, headers=headers)
        parsed = json.loads(payload_raw)
    except Exception:
        return "", None, None
    if not isinstance(parsed, dict):
        return "", None, None
    html_url = str(parsed.get("html_url") or "").strip() or None
    content = parsed.get("content")
    encoding = str(parsed.get("encoding") or "").lower()
    if isinstance(content, str) and encoding == "base64":
        try:
            decoded = base64.b64decode(content.encode("utf-8"), validate=False).decode("utf-8", errors="replace")
            return _trim_doc(decoded), html_url, "github_api"
        except Exception:
            return "", html_url, "github_api"
    return "", html_url, "github_api"


def _fetch_with_openclaw(repo_url: str, timeout: int) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    if not repo_url:
        return "", None, None, None
    client = OpenClawClient(timeout_seconds=max(4, timeout))
    if not client.available:
        return "", None, None, None
    try:
        result = client.run_skill(
            "github.fetch",
            {
                "repo_url": repo_url,
                "depth": 1,
                "include_submodules": False,
                "include_lfs": False,
                "max_files": 800,
            },
        )
    except OpenClawClientError as exc:
        return "", None, None, str(exc)
    payload = result.payload if isinstance(result.payload, dict) else {}
    if payload.get("ok") is False:
        return "", None, None, str(payload.get("error_message") or "openclaw skill failed")
    output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
    readme = _trim_doc(str(output.get("readme_rendered") or ""))
    url = str(output.get("repo_url") or repo_url).strip() or repo_url
    return readme, url, "openclaw_github_fetch", None


def fetch_repo_document(
    item: Dict[str, Any],
    timeout: int = 10,
) -> Dict[str, Any]:
    started = time.perf_counter()
    repo_url = str(item.get("html_url") or "").strip()
    source = str(item.get("source") or "").strip().lower()
    full_name = _repo_full_name(item)
    warnings: List[str] = []

    if source == "github" and OPENCLAW_BASE_URL:
        readme, url, fetch_source, error = _fetch_with_openclaw(repo_url, timeout=timeout)
        if readme:
            duration_ms = int((time.perf_counter() - started) * 1000)
            record_timing_metric(name="recommend.deep_fetch.openclaw.latency_ms", duration_ms=duration_ms)
            return {
                "content": readme,
                "url": url or repo_url,
                "fetch_source": fetch_source,
                "warnings": warnings,
                "duration_ms": duration_ms,
            }
        if error:
            warnings.append(f"openclaw fallback: {error}")
            record_counter_metric(name="recommend.deep_fetch.openclaw.fallback", value=1)

    if source == "github":
        readme, url, fetch_source = _fetch_github_readme_via_api(full_name, timeout=timeout)
        if readme:
            duration_ms = int((time.perf_counter() - started) * 1000)
            record_timing_metric(name="recommend.deep_fetch.github_api.latency_ms", duration_ms=duration_ms)
            return {
                "content": readme,
                "url": url or repo_url,
                "fetch_source": fetch_source,
                "warnings": warnings,
                "duration_ms": duration_ms,
            }

    for candidate_url in _readme_candidates(item):
        try:
            text = _http_get_text(candidate_url, timeout=timeout, headers={"User-Agent": "AntiHub/0.5"})
        except urllib.error.HTTPError:
            continue
        except Exception:
            continue
        if candidate_url.endswith(".md") or candidate_url.endswith(".rst") or "raw" in candidate_url:
            markdown = _trim_doc(text)
            if markdown:
                duration_ms = int((time.perf_counter() - started) * 1000)
                record_timing_metric(name="recommend.deep_fetch.native_readme.latency_ms", duration_ms=duration_ms)
                return {
                    "content": markdown,
                    "url": candidate_url,
                    "fetch_source": "native_readme",
                    "warnings": warnings,
                    "duration_ms": duration_ms,
                }
        cleaned = _trim_doc(_clean_html_to_text(text))
        if cleaned:
            duration_ms = int((time.perf_counter() - started) * 1000)
            record_timing_metric(name="recommend.deep_fetch.native_web.latency_ms", duration_ms=duration_ms)
            return {
                "content": cleaned,
                "url": candidate_url,
                "fetch_source": "native_web_snapshot",
                "warnings": warnings,
                "duration_ms": duration_ms,
            }

    warnings.append("document fetch failed")
    record_counter_metric(name="recommend.deep_fetch.failed", value=1)
    return {
        "content": "",
        "url": repo_url or None,
        "fetch_source": None,
        "warnings": warnings,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def enrich_candidates_with_documents(
    candidates: List[Dict[str, Any]],
    *,
    top_n: int = 4,
    timeout: int = 10,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> List[str]:
    warnings: List[str] = []
    if not candidates:
        return warnings
    picks = candidates[: max(1, min(int(top_n), len(candidates)))]
    with ThreadPoolExecutor(max_workers=min(4, len(picks))) as pool:
        futures = {pool.submit(fetch_repo_document, item, timeout): item for item in picks}
        for future in as_completed(futures):
            item = futures[future]
            name = str(item.get("full_name") or item.get("id") or "")
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{name} 文档抓取失败: {exc}")
                continue
            content = str(result.get("content") or "").strip()
            item["doc_excerpt"] = _trim_doc(content, limit=2000) if content else ""
            item["doc_markdown"] = _trim_doc(content, limit=3000) if content else ""
            item["doc_url"] = str(result.get("url") or "").strip() or None
            item["doc_fetch_source"] = str(result.get("fetch_source") or "").strip() or None
            item["doc_fetch_duration_ms"] = int(result.get("duration_ms") or 0)
            for warning in result.get("warnings") or []:
                text = str(warning or "").strip()
                if text:
                    warnings.append(f"{name}: {text}")
            if progress_callback:
                try:
                    if content:
                        progress_callback(
                            f"抓取文档成功：{name}（{item.get('doc_fetch_source') or 'native'}，{item.get('doc_fetch_duration_ms', 0)}ms）"
                        )
                    else:
                        progress_callback(f"抓取文档失败：{name}（已记录回退）")
                except Exception:
                    continue
    return warnings
