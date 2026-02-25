import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from config import (
    OPENCLAW_API_KEY,
    OPENCLAW_BASE_URL,
    OPENCLAW_SKILL_ENDPOINT,
    OPENCLAW_TIMEOUT_SECONDS,
)


class OpenClawClientError(RuntimeError):
    pass


@dataclass
class OpenClawResult:
    payload: Dict[str, Any]
    duration_ms: int
    endpoint: str


class OpenClawClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        skill_endpoint: Optional[str] = None,
    ) -> None:
        self._base_url = (base_url or OPENCLAW_BASE_URL or "").strip().rstrip("/")
        self._api_key = (api_key or OPENCLAW_API_KEY or "").strip()
        self._timeout = int(timeout_seconds or OPENCLAW_TIMEOUT_SECONDS)
        self._skill_endpoint = (skill_endpoint or OPENCLAW_SKILL_ENDPOINT or "").strip()

    @property
    def available(self) -> bool:
        return bool(self._base_url)

    def run_skill(self, skill: str, payload: Dict[str, Any]) -> OpenClawResult:
        if not self._base_url:
            raise OpenClawClientError("OPENCLAW_BASE_URL is missing")
        endpoint = self._skill_endpoint or "/skills/run"
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        url = f"{self._base_url}{endpoint}"
        body = json.dumps({"skill": skill, "input": payload}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        host = (urlparse(self._base_url).hostname or "").lower()
        bypass_proxy = host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.startswith("127.")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if bypass_proxy else None
        start = time.time()
        try:
            if opener:
                resp_ctx = opener.open(request, timeout=self._timeout)
            else:
                resp_ctx = urllib.request.urlopen(request, timeout=self._timeout)
            with resp_ctx as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise OpenClawClientError(f"openclaw request failed: {exc.code} {detail}") from exc
        except Exception as exc:
            raise OpenClawClientError(f"openclaw request failed: {exc}") from exc
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise OpenClawClientError(f"openclaw response parse failed: {exc}") from exc
        duration_ms = int((time.time() - start) * 1000)
        return OpenClawResult(payload=data, duration_ms=duration_ms, endpoint=endpoint)
