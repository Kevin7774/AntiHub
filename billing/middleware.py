from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Final

import redis

from config import APP_ENV, REDIS_DISABLED, REDIS_URL
from observability import get_logger, log_event

from .db import session_scope
from .repository import BillingRepository

RATE_LIMIT_FREE_RPM: Final[int] = 5
RATE_LIMIT_MONTHLY_RPM: Final[int] = 50
RATE_LIMIT_QUARTERLY_RPM: Final[int] = 100
RATE_LIMIT_YEARLY_RPM: Final[int] = 500
RATE_LIMIT_CACHE_TTL_SECONDS: Final[float] = 30.0

RATE_LIMIT_REDIS_KEY_PREFIX: Final[str] = "billing:rate_limit:"
RATE_LIMIT_LUA_SCRIPT: Final[str] = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_per_sec = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local values = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(values[1])
local ts = tonumber(values[2])

if tokens == nil then
  tokens = capacity
end
if ts == nil then
  ts = now
end

if now > ts then
  local delta = now - ts
  tokens = math.min(capacity, tokens + (delta * refill_per_sec))
end

local allowed = 0
local retry_after = 0

if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  local missing = cost - tokens
  retry_after = math.ceil(missing / refill_per_sec)
end

redis.call("HSET", key, "tokens", tokens, "ts", now)
redis.call("EXPIRE", key, math.max(120, math.ceil((capacity / refill_per_sec) * 2)))

return {allowed, tokens, retry_after}
"""

_LOGGER = get_logger("antihub.billing.rate_limit")


def _is_production_env() -> bool:
    return str(APP_ENV or "").strip().lower() in {"prod", "production"}


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit_rpm: int
    remaining: int
    retry_after_seconds: int


def resolve_plan_rpm(plan_code: str | None) -> int:
    code = str(plan_code or "").strip().lower()
    if not code:
        return RATE_LIMIT_FREE_RPM
    if "year" in code or "annual" in code:
        return RATE_LIMIT_YEARLY_RPM
    if "quarter" in code or "season" in code:
        return RATE_LIMIT_QUARTERLY_RPM
    if "month" in code:
        return RATE_LIMIT_MONTHLY_RPM
    # Unknown paid plan: keep it conservative but still higher than free tier.
    return RATE_LIMIT_MONTHLY_RPM


class _RateCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, int]] = {}

    def get(self, username: str) -> int | None:
        now = time.time()
        with self._lock:
            value = self._cache.get(username)
            if not value:
                return None
            expires_at, rpm = value
            if expires_at <= now:
                self._cache.pop(username, None)
                return None
            return rpm

    def set(self, username: str, rpm: int) -> None:
        with self._lock:
            self._cache[username] = (time.time() + RATE_LIMIT_CACHE_TTL_SECONDS, int(rpm))


_RATE_CACHE = _RateCache()


def resolve_user_rpm(username: str) -> int:
    user_key = str(username or "").strip()
    if not user_key:
        return RATE_LIMIT_FREE_RPM

    cached = _RATE_CACHE.get(user_key)
    if cached is not None:
        return cached

    rpm = RATE_LIMIT_FREE_RPM
    try:
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.expire_due_subscriptions()
            subscription = repo.get_active_subscription(user_key)
            plan_code = None
            if subscription is not None and getattr(subscription, "plan", None) is not None:
                plan_code = getattr(subscription.plan, "code", None)
            rpm = resolve_plan_rpm(str(plan_code or ""))
    except Exception as exc:  # noqa: BLE001
        log_event(
            _LOGGER,
            30,
            "rate_limit.resolve_user_rpm_fallback",
            user_id=user_key,
            error=str(exc),
        )
        rpm = RATE_LIMIT_FREE_RPM

    _RATE_CACHE.set(user_key, rpm)
    return rpm


class BillingRateLimiter:
    def __init__(self) -> None:
        self._client = self._build_redis_client()
        self._memory_lock = threading.Lock()
        self._memory_buckets: dict[str, tuple[float, float]] = {}
        if self._client is None:
            log_event(
                _LOGGER,
                40 if _is_production_env() else 30,
                "rate_limit.init_no_redis_using_memory",
                production=_is_production_env(),
            )

    @staticmethod
    def _build_redis_client() -> redis.Redis | None:
        if REDIS_DISABLED or str(REDIS_URL).startswith("memory://"):
            return None
        try:
            client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            client.ping()
            return client
        except Exception as exc:  # noqa: BLE001
            log_event(
                _LOGGER,
                40 if _is_production_env() else 30,
                "rate_limit.redis_unavailable_fallback_memory",
                redis_url=REDIS_URL,
                error=str(exc),
            )
            return None

    def allow(self, *, subject: str, limit_rpm: int, cost: int = 1) -> RateLimitResult:
        capacity = max(1, int(limit_rpm))
        spend = max(1, int(cost))
        key = f"{RATE_LIMIT_REDIS_KEY_PREFIX}{subject}"

        if self._client is not None:
            try:
                return self._allow_redis(key=key, capacity=capacity, cost=spend)
            except Exception as exc:  # noqa: BLE001
                # Redis outage: degrade to in-process limiting instead of failing requests.
                log_event(
                    _LOGGER,
                    40,
                    "rate_limit.redis_call_failed_fallback_memory",
                    key=key,
                    error=str(exc),
                )
                self._client = None
        return self._allow_memory(key=key, capacity=capacity, cost=spend)

    def _allow_redis(self, *, key: str, capacity: int, cost: int) -> RateLimitResult:
        if self._client is None:
            return self._allow_memory(key=key, capacity=capacity, cost=cost)

        refill_per_second = capacity / 60.0
        now = time.time()
        raw = self._client.eval(
            RATE_LIMIT_LUA_SCRIPT,
            1,
            key,
            str(now),
            str(capacity),
            str(refill_per_second),
            str(cost),
        )
        if not isinstance(raw, list) or len(raw) < 3:
            raise RuntimeError("invalid redis rate limit response")

        allowed = int(raw[0]) == 1
        tokens_remaining = max(0.0, float(raw[1]))
        retry_after = max(0, int(raw[2]))
        remaining_int = max(0, int(math.floor(tokens_remaining)))
        return RateLimitResult(
            allowed=allowed,
            limit_rpm=capacity,
            remaining=remaining_int,
            retry_after_seconds=retry_after,
        )

    def _allow_memory(self, *, key: str, capacity: int, cost: int) -> RateLimitResult:
        refill_per_second = capacity / 60.0
        now = time.time()

        with self._memory_lock:
            tokens, last_seen = self._memory_buckets.get(key, (float(capacity), now))
            elapsed = max(0.0, now - last_seen)
            tokens = min(float(capacity), tokens + elapsed * refill_per_second)

            allowed = tokens >= cost
            retry_after = 0
            if allowed:
                tokens -= cost
            else:
                missing = float(cost) - tokens
                retry_after = max(1, int(math.ceil(missing / refill_per_second)))

            self._memory_buckets[key] = (tokens, now)
            remaining_int = max(0, int(math.floor(tokens)))

        return RateLimitResult(
            allowed=allowed,
            limit_rpm=capacity,
            remaining=remaining_int,
            retry_after_seconds=retry_after,
        )
