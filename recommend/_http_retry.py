"""Shared HTTP retry helper for search providers."""

from __future__ import annotations

import time
import urllib.error
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Retryable HTTP status codes (server errors + rate-limit proxy errors).
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504, 520, 521, 522, 523, 524}


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception is a transient error worth retrying."""
    # urllib exceptions
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_STATUS_CODES
    if isinstance(exc, (urllib.error.URLError, OSError, TimeoutError, ConnectionError)):
        return True
    # httpx exceptions (checked by attribute to avoid hard import dependency)
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code is not None and status_code in _RETRYABLE_STATUS_CODES:
        return True
    cls_name = type(exc).__name__
    if cls_name in {"ConnectError", "ReadTimeout", "ConnectTimeout", "PoolTimeout"}:
        return True
    return False


def with_retry(
    fn: Callable[..., T],
    *args: Any,
    max_retries: int = 2,
    initial_delay: float = 1.0,
    **kwargs: Any,
) -> T:
    """Execute *fn* with automatic retry on transient HTTP errors.

    Uses exponential backoff: 1s, 2s (for max_retries=2).
    Only retries on 5xx errors and connection/timeout failures.
    4xx errors (rate-limit, auth, validation) are never retried.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= max_retries or not _is_retryable(exc):
                raise
            delay = initial_delay * (2 ** attempt)
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]  # unreachable but keeps mypy happy
