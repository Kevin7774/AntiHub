import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import (
    VISUAL_API_KEY,
    VISUAL_BASE_URL,
    VISUAL_IMAGE_ENDPOINT,
    VISUAL_IMAGE_MODEL,
    VISUAL_IMAGE_SIZE,
    VISUAL_IMAGE_TIMEOUT_SECONDS,
    VISUAL_PROVIDER,
)


class ImageClientError(RuntimeError):
    pass


@dataclass
class ImageResult:
    image_bytes: bytes
    raw: Dict[str, Any]
    duration_ms: int
    endpoint: str
    model: str


class ImageClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        endpoint: Optional[str] = None,
        default_size: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self._api_key = (api_key or VISUAL_API_KEY or "").strip()
        self._base_url = self._normalize_base_url(base_url or VISUAL_BASE_URL or "")
        self._model = (model or VISUAL_IMAGE_MODEL or "").strip()
        self._provider = (provider or VISUAL_PROVIDER or "minimax").strip().lower()
        self._endpoint = (endpoint or VISUAL_IMAGE_ENDPOINT or "").strip()
        self._default_size = (default_size or VISUAL_IMAGE_SIZE or "").strip()
        self._timeout = int(timeout or VISUAL_IMAGE_TIMEOUT_SECONDS)

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        return base

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._api_key:
            raise ImageClientError("VISUAL_API_KEY is missing")
        if not self._base_url:
            raise ImageClientError("VISUAL_BASE_URL is missing")
        endpoint_value = endpoint or ""
        base = self._base_url.rstrip("/")
        if base.endswith("/v1") and endpoint_value.startswith("/v1"):
            endpoint_value = endpoint_value[3:]
        url = f"{base}{endpoint_value}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                data = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise ImageClientError(f"image request failed: {exc.code} {detail}") from exc
        except Exception as exc:
            raise ImageClientError(f"image request failed: {exc}") from exc
        try:
            return json.loads(data)
        except Exception as exc:
            raise ImageClientError(f"image response parse failed: {exc}") from exc

    def _extract_image_bytes(self, payload: Dict[str, Any]) -> bytes:
        if "data" in payload and isinstance(payload["data"], list) and payload["data"]:
            item = payload["data"][0]
            if isinstance(item, dict):
                b64 = item.get("b64_json") or item.get("image") or item.get("base64")
                if b64:
                    return base64.b64decode(b64)
                url = item.get("url")
                if url:
                    return self._fetch_image(url)
        if "image" in payload and isinstance(payload["image"], str):
            return base64.b64decode(payload["image"])
        if "images" in payload and isinstance(payload["images"], list) and payload["images"]:
            first = payload["images"][0]
            if isinstance(first, str):
                return base64.b64decode(first)
            if isinstance(first, dict):
                b64 = first.get("base64") or first.get("b64_json") or first.get("image")
                if b64:
                    return base64.b64decode(b64)
                url = first.get("url")
                if url:
                    return self._fetch_image(url)
        if "output" in payload and isinstance(payload["output"], dict):
            image = payload["output"].get("image")
            if isinstance(image, str):
                return base64.b64decode(image)
        raise ImageClientError("image response missing image data")

    def _fetch_image(self, url: str) -> bytes:
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                return resp.read()
        except Exception as exc:
            raise ImageClientError(f"image download failed: {exc}") from exc

    def generate(self, prompt: str, image_b64: Optional[str] = None, seed: Optional[int] = None) -> ImageResult:
        if not self._model:
            raise ImageClientError("VISUAL_IMAGE_MODEL is missing")
        endpoint = self._endpoint
        if not endpoint:
            if self._provider in {"openai", "openai_compatible", "openai-compatible"}:
                endpoint = "/images/generations"
            else:
                endpoint = "/v1/image_generation"
        payload: Dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
        }
        if self._default_size:
            payload["size"] = self._default_size
        if seed is not None:
            payload["seed"] = seed
        if image_b64:
            payload["image"] = image_b64
        payload.setdefault("response_format", "b64_json")

        start = time.time()
        response = self._post(endpoint, payload)
        duration_ms = int((time.time() - start) * 1000)
        image_bytes = self._extract_image_bytes(response)
        return ImageResult(
            image_bytes=image_bytes,
            raw=response,
            duration_ms=duration_ms,
            endpoint=endpoint,
            model=self._model,
        )
