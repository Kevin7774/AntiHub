from __future__ import annotations

import json
import threading
import time
from typing import Any

import redis
from fastapi import HTTPException, Request

from config import FEATURE_SAAS_ENTITLEMENTS, REDIS_DISABLED, REDIS_URL

from .db import SessionFactory, session_scope
from .repository import BillingRepository

ENTITLEMENTS_CACHE_TTL_SECONDS = 60.0
ENTITLEMENTS_CACHE_KEY_PREFIX = "billing:entitlements:user:"


class _EntitlementsCache:
    def __init__(self) -> None:
        self._memory_lock = threading.Lock()
        self._memory_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._redis_client = self._build_redis_client()

    @staticmethod
    def _build_redis_client() -> redis.Redis | None:
        if REDIS_DISABLED or str(REDIS_URL or "").startswith("memory://"):
            return None
        try:
            client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            client.ping()
            return client
        except Exception:
            return None

    def get(self, user_id: str) -> dict[str, Any] | None:
        key = f"{ENTITLEMENTS_CACHE_KEY_PREFIX}{user_id}"
        if self._redis_client is not None:
            try:
                raw = self._redis_client.get(key)
                if raw:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        return payload
            except Exception:
                self._redis_client = None

        now = time.time()
        with self._memory_lock:
            cached = self._memory_cache.get(key)
            if cached is None:
                return None
            expires_at, payload = cached
            if expires_at <= now:
                self._memory_cache.pop(key, None)
                return None
            return dict(payload)

    def set(self, user_id: str, payload: dict[str, Any], ttl_seconds: float = ENTITLEMENTS_CACHE_TTL_SECONDS) -> None:
        key = f"{ENTITLEMENTS_CACHE_KEY_PREFIX}{user_id}"
        encoded = json.dumps(payload, ensure_ascii=False)
        if self._redis_client is not None:
            try:
                self._redis_client.setex(key, max(1, int(ttl_seconds)), encoded)
                return
            except Exception:
                self._redis_client = None

        with self._memory_lock:
            self._memory_cache[key] = (time.time() + max(1.0, float(ttl_seconds)), dict(payload))

    def delete(self, user_id: str) -> None:
        key = f"{ENTITLEMENTS_CACHE_KEY_PREFIX}{user_id}"
        if self._redis_client is not None:
            try:
                self._redis_client.delete(key)
            except Exception:
                self._redis_client = None

        with self._memory_lock:
            self._memory_cache.pop(key, None)


_CACHE = _EntitlementsCache()


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


def _load_user_entitlements(user_id: str, *, session_factory: SessionFactory | None = None) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        repo.expire_due_subscriptions()
        subscription = repo.get_active_subscription(user_id)
        if subscription is None:
            return {}

        plan_id = str(getattr(subscription, "plan_id", "") or "").strip()
        if not plan_id:
            return {}

        rows = repo.list_plan_entitlements(plan_id=plan_id, include_disabled=True)
        payload: dict[str, Any] = {}
        for row in rows:
            key = str(getattr(row, "key", "") or "").strip()
            if not key:
                continue
            payload[key] = {
                "enabled": bool(getattr(row, "enabled", False)),
                "value": _normalize_value(getattr(row, "value_json", None)),
                "limit": (
                    int(getattr(row, "limit_value"))
                    if getattr(row, "limit_value", None) is not None
                    else None
                ),
                "metadata": _normalize_value(getattr(row, "metadata_json", None)) or {},
            }
        return payload


def get_user_entitlements(
    user_id: str,
    *,
    session_factory: SessionFactory | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return {}

    if not force_refresh:
        cached = _CACHE.get(normalized_user_id)
        if cached is not None:
            return cached

    payload = _load_user_entitlements(normalized_user_id, session_factory=session_factory)
    _CACHE.set(normalized_user_id, payload)
    return payload


def invalidate_user_entitlements(user_id: str) -> None:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return
    _CACHE.delete(normalized_user_id)


def invalidate_plan_entitlements(plan_id: str, *, session_factory: SessionFactory | None = None) -> int:
    normalized_plan_id = str(plan_id or "").strip()
    if not normalized_plan_id:
        return 0
    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        user_ids = repo.list_active_subscription_user_ids_by_plan(plan_id=normalized_plan_id)
    for user_id in user_ids:
        _CACHE.delete(user_id)
    return len(user_ids)


def require_entitlement(entitlement_key: str):
    normalized_key = str(entitlement_key or "").strip()

    async def _dependency(request: Request) -> dict[str, Any]:
        if not FEATURE_SAAS_ENTITLEMENTS:
            raise HTTPException(status_code=404, detail="feature disabled")

        identity = getattr(request.state, "auth_identity", None)
        user_id = str(getattr(identity, "username", "") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="unauthorized")

        entitlements = get_user_entitlements(user_id)
        item = entitlements.get(normalized_key)
        if not isinstance(item, dict) or not bool(item.get("enabled", False)):
            raise HTTPException(status_code=403, detail="entitlement required")
        return item

    return _dependency
