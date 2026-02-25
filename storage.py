import asyncio
import json
import re
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Union

import redis
import redis.asyncio as redis_async
from sqlalchemy import Float, Integer, String, Text, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from billing.db import build_session_factory
from config import (
    ANALYZE_LOCK_PREFIX,
    CASE_PREFIX,
    CASE_STORE_BACKEND,
    CASE_STORE_DATABASE_URL,
    LOG_LIST_PREFIX,
    LOG_RETENTION_LINES,
    MANUAL_META_PREFIX,
    MANUAL_PREFIX,
    MANUAL_STATS_KEY,
    MANUAL_STATUS_PREFIX,
    REDIS_DISABLED,
    REDIS_URL,
    VISUAL_LOCK_PREFIX,
    WS_LOG_CHANNEL_PREFIX,
)

LogRaw = Union[str, bytes, Dict[str, Any]]

SECRET_PAIR_PATTERN = re.compile(
    r"(?P<key>[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
SECRET_JSON_PATTERN = re.compile(
    r"(?P<key>\"[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\"\s*:\s*)\"(?P<value>[^\"]+)\"",
    re.IGNORECASE,
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._\-~+/]+=*)")
SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")

USE_MEMORY_STORE = REDIS_DISABLED or str(REDIS_URL).startswith("memory://")
USE_DB_CASE_STORE = CASE_STORE_BACKEND in {"database", "db", "sql", "sqlalchemy"}
_MEM_CASES: Dict[str, Dict[str, Any]] = {}
_MEM_LOGS: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
_MEM_MANUAL: Dict[str, str] = {}
_MEM_MANUAL_META: Dict[str, Dict[str, Any]] = {}
_MEM_MANUAL_STATUS: Dict[str, Dict[str, Any]] = {}
_MEM_STATS: Dict[str, int] = {}
_MEM_LOCKS: Dict[str, float] = {}


class RuntimeStoreBase(DeclarativeBase):
    pass


class RuntimeKV(RuntimeStoreBase):
    __tablename__ = "runtime_kv_store"

    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(Float, default=lambda: time.time(), index=True)


class RuntimeLog(RuntimeStoreBase):
    __tablename__ = "runtime_log_store"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=lambda: time.time(), index=True)


_DB_INIT_LOCK = Lock()
_DB_READY = False
_DB_SESSION_FACTORY: sessionmaker[Session] | None = None


def _purge_expired_locks() -> None:
    now = time.time()
    expired = [key for key, expires in _MEM_LOCKS.items() if expires <= now]
    for key in expired:
        _MEM_LOCKS.pop(key, None)


def _memory_lock(key: str, ttl_seconds: int) -> bool:
    _purge_expired_locks()
    if key in _MEM_LOCKS:
        return False
    _MEM_LOCKS[key] = time.time() + max(1, ttl_seconds)
    return True


def _memory_unlock(key: str) -> None:
    _MEM_LOCKS.pop(key, None)


class _MemoryPubSub:
    async def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def get_message(self, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0)
        return None

    async def unsubscribe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def close(self) -> None:
        return None


class _MemoryAsyncRedis:
    def pubsub(self) -> _MemoryPubSub:
        return _MemoryPubSub()

    async def close(self) -> None:
        return None


def _utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _ensure_runtime_db_ready() -> None:
    global _DB_READY
    global _DB_SESSION_FACTORY
    if not USE_DB_CASE_STORE:
        return
    if _DB_READY and _DB_SESSION_FACTORY is not None:
        return
    with _DB_INIT_LOCK:
        if _DB_READY and _DB_SESSION_FACTORY is not None:
            return
        engine, session_factory = build_session_factory(CASE_STORE_DATABASE_URL)
        RuntimeStoreBase.metadata.create_all(bind=engine)
        _DB_SESSION_FACTORY = session_factory
        _DB_READY = True


@contextmanager
def _runtime_session_scope() -> Any:
    _ensure_runtime_db_ready()
    if _DB_SESSION_FACTORY is None:
        raise RuntimeError("runtime DB session factory is not initialized")
    session = _DB_SESSION_FACTORY()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _db_get_kv(namespace: str, key: str) -> str | None:
    with _runtime_session_scope() as session:
        row = session.get(RuntimeKV, {"namespace": namespace, "key": key})
        if row is None:
            return None
        return str(row.value)


def _db_set_kv(namespace: str, key: str, value: str) -> None:
    with _runtime_session_scope() as session:
        row = session.get(RuntimeKV, {"namespace": namespace, "key": key})
        if row is None:
            row = RuntimeKV(namespace=namespace, key=key, value=value, updated_at=_utc_ts())
            session.add(row)
            return
        row.value = value
        row.updated_at = _utc_ts()


def _db_list_keys(namespace: str) -> list[str]:
    with _runtime_session_scope() as session:
        query = select(RuntimeKV.key).where(RuntimeKV.namespace == namespace).order_by(RuntimeKV.key.asc())
        return [str(item) for item in session.scalars(query).all()]


def _db_append_log(case_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False)
    with _runtime_session_scope() as session:
        session.add(RuntimeLog(case_id=case_id, payload=encoded, created_at=float(payload.get("ts") or _utc_ts())))
        session.flush()

        total = int(
            session.scalar(
                select(func.count(RuntimeLog.id)).where(RuntimeLog.case_id == case_id)
            )
            or 0
        )
        if total > LOG_RETENTION_LINES:
            remove_count = total - LOG_RETENTION_LINES
            stale_ids = list(
                session.scalars(
                    select(RuntimeLog.id)
                    .where(RuntimeLog.case_id == case_id)
                    .order_by(RuntimeLog.id.asc())
                    .limit(remove_count)
                ).all()
            )
            if stale_ids:
                session.execute(delete(RuntimeLog).where(RuntimeLog.id.in_(stale_ids)))
    return payload


def _db_get_logs(case_id: str) -> list[Dict[str, Any]]:
    with _runtime_session_scope() as session:
        rows = session.scalars(
            select(RuntimeLog.payload).where(RuntimeLog.case_id == case_id).order_by(RuntimeLog.id.asc())
        ).all()
        return [_decode_log_entry(item) for item in rows]


def _publish_log_payload(case_id: str, payload: Dict[str, Any]) -> None:
    if USE_MEMORY_STORE:
        return
    try:
        client = get_redis_client()
        client.publish(log_channel(case_id), json.dumps(payload, ensure_ascii=False))
    except Exception:
        return

def get_redis_client() -> redis.Redis:
    if USE_MEMORY_STORE:
        raise RuntimeError("redis disabled")
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def get_async_redis_client() -> redis_async.Redis:
    if USE_MEMORY_STORE:
        return _MemoryAsyncRedis()  # type: ignore[return-value]
    return redis_async.Redis.from_url(REDIS_URL, decode_responses=True)


def set_case(case_id: str, data: Dict[str, Any]) -> None:
    if USE_DB_CASE_STORE:
        _db_set_kv("case", case_id, json.dumps(dict(data), ensure_ascii=False))
        return
    if USE_MEMORY_STORE:
        _MEM_CASES[case_id] = dict(data)
        return
    client = get_redis_client()
    client.hset(f"{CASE_PREFIX}{case_id}", mapping=_encode_data(data))


def update_case(case_id: str, data: Dict[str, Any]) -> None:
    if not data:
        return
    data = dict(data)
    data["updated_at"] = time.time()
    if USE_DB_CASE_STORE:
        existing = get_case(case_id) or {}
        existing.update(data)
        _db_set_kv("case", case_id, json.dumps(existing, ensure_ascii=False))
        return
    if USE_MEMORY_STORE:
        _MEM_CASES.setdefault(case_id, {}).update(data)
        return
    client = get_redis_client()
    client.hset(f"{CASE_PREFIX}{case_id}", mapping=_encode_data(data))


def get_case(case_id: str) -> Dict[str, Any] | None:
    if USE_DB_CASE_STORE:
        raw = _db_get_kv("case", case_id)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return dict(payload)
    if USE_MEMORY_STORE:
        data = _MEM_CASES.get(case_id)
        return dict(data) if data else None
    client = get_redis_client()
    raw = client.hgetall(f"{CASE_PREFIX}{case_id}")
    if not raw:
        return None
    return {key: json.loads(value) for key, value in raw.items()}


def list_case_ids() -> List[str]:
    if USE_DB_CASE_STORE:
        return _db_list_keys("case")
    if USE_MEMORY_STORE:
        return list(_MEM_CASES.keys())
    client = get_redis_client()
    cursor = 0
    keys: List[str] = []
    pattern = f"{CASE_PREFIX}*"
    while True:
        cursor, batch = client.scan(cursor=cursor, match=pattern, count=200)
        keys.extend(batch)
        if cursor == 0:
            break
    return [key.replace(CASE_PREFIX, "", 1) for key in keys]


def append_log(case_id: str, message: LogRaw) -> Dict[str, Any]:
    payload = _decode_log_entry(message)
    if USE_DB_CASE_STORE:
        _db_append_log(case_id, payload)
        update_case(case_id, {"last_log_at": payload["ts"]})
        _publish_log_payload(case_id, payload)
        return payload
    if USE_MEMORY_STORE:
        entries = _MEM_LOGS[case_id]
        entries.append(payload)
        if len(entries) > LOG_RETENTION_LINES:
            _MEM_LOGS[case_id] = entries[-LOG_RETENTION_LINES:]
        _MEM_CASES.setdefault(case_id, {})["last_log_at"] = payload["ts"]
        return payload
    client = get_redis_client()
    key = f"{LOG_LIST_PREFIX}{case_id}"
    client.rpush(key, json.dumps(payload, ensure_ascii=False))
    client.ltrim(key, -LOG_RETENTION_LINES, -1)
    client.hset(f"{CASE_PREFIX}{case_id}", mapping={"last_log_at": str(payload["ts"])})
    client.publish(log_channel(case_id), json.dumps(payload, ensure_ascii=False))
    return payload


def get_logs(case_id: str) -> List[Dict[str, Any]]:
    if USE_DB_CASE_STORE:
        return _db_get_logs(case_id)
    if USE_MEMORY_STORE:
        return list(_MEM_LOGS.get(case_id, []))
    client = get_redis_client()
    entries = client.lrange(f"{LOG_LIST_PREFIX}{case_id}", 0, -1)
    return [_decode_log_entry(entry) for entry in entries]


def get_logs_slice(case_id: str, offset: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    if offset < 0:
        offset = 0
    entries = get_logs(case_id)
    if not entries:
        return []
    end = len(entries) - offset
    if end <= 0:
        return []
    start = max(0, end - limit)
    return entries[start:end]


def log_channel(case_id: str) -> str:
    return f"{WS_LOG_CHANNEL_PREFIX}{case_id}"


def acquire_analyze_lock(cache_key: str, ttl_seconds: int) -> bool:
    if USE_MEMORY_STORE:
        return _memory_lock(f"{ANALYZE_LOCK_PREFIX}{cache_key}", ttl_seconds)
    client = get_redis_client()
    return bool(client.set(f"{ANALYZE_LOCK_PREFIX}{cache_key}", "1", nx=True, ex=ttl_seconds))


def release_analyze_lock(cache_key: str) -> None:
    if USE_MEMORY_STORE:
        _memory_unlock(f"{ANALYZE_LOCK_PREFIX}{cache_key}")
        return
    client = get_redis_client()
    client.delete(f"{ANALYZE_LOCK_PREFIX}{cache_key}")


def acquire_visualize_lock(cache_key: str, ttl_seconds: int) -> bool:
    if USE_MEMORY_STORE:
        return _memory_lock(f"{VISUAL_LOCK_PREFIX}{cache_key}", ttl_seconds)
    client = get_redis_client()
    return bool(client.set(f"{VISUAL_LOCK_PREFIX}{cache_key}", "1", nx=True, ex=ttl_seconds))


def release_visualize_lock(cache_key: str) -> None:
    if USE_MEMORY_STORE:
        _memory_unlock(f"{VISUAL_LOCK_PREFIX}{cache_key}")
        return
    client = get_redis_client()
    client.delete(f"{VISUAL_LOCK_PREFIX}{cache_key}")


def decode_log_entry(raw: str | dict) -> Dict[str, Any]:
    return _decode_log_entry(raw)


def _encode_data(data: Dict[str, Any]) -> Dict[str, str]:
    return {key: json.dumps(value) for key, value in data.items()}


def set_manual(case_id: str, markdown: str, meta: Dict[str, Any]) -> None:
    if USE_DB_CASE_STORE:
        _db_set_kv("manual", case_id, str(markdown or ""))
        _db_set_kv("manual_meta", case_id, json.dumps(meta or {}, ensure_ascii=False))
        return
    if USE_MEMORY_STORE:
        _MEM_MANUAL[case_id] = markdown
        _MEM_MANUAL_META[case_id] = dict(meta)
        return
    client = get_redis_client()
    client.set(f"{MANUAL_PREFIX}{case_id}", markdown)
    client.set(f"{MANUAL_META_PREFIX}{case_id}", json.dumps(meta))


def get_manual(case_id: str) -> tuple[str | None, Dict[str, Any] | None]:
    if USE_DB_CASE_STORE:
        markdown = _db_get_kv("manual", case_id)
        meta_raw = _db_get_kv("manual_meta", case_id)
        if not meta_raw:
            return markdown, None
        try:
            meta = json.loads(meta_raw)
        except Exception:
            meta = None
        if meta is not None and not isinstance(meta, dict):
            meta = None
        return markdown, meta
    if USE_MEMORY_STORE:
        return _MEM_MANUAL.get(case_id), _MEM_MANUAL_META.get(case_id)
    client = get_redis_client()
    markdown = client.get(f"{MANUAL_PREFIX}{case_id}")
    meta_raw = client.get(f"{MANUAL_META_PREFIX}{case_id}")
    meta = json.loads(meta_raw) if meta_raw else None
    return markdown, meta


def set_manual_status(
    case_id: str,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    generated_at: float | None = None,
) -> None:
    payload = {
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
        "generated_at": generated_at,
        "updated_at": time.time(),
    }
    if USE_DB_CASE_STORE:
        _db_set_kv("manual_status", case_id, json.dumps(payload, ensure_ascii=False))
        return
    if USE_MEMORY_STORE:
        _MEM_MANUAL_STATUS[case_id] = payload
        return
    client = get_redis_client()
    client.hset(f"{MANUAL_STATUS_PREFIX}{case_id}", mapping=_encode_data(payload))


def get_manual_status(case_id: str) -> Dict[str, Any] | None:
    if USE_DB_CASE_STORE:
        raw = _db_get_kv("manual_status", case_id)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return payload
    if USE_MEMORY_STORE:
        data = _MEM_MANUAL_STATUS.get(case_id)
        return dict(data) if data else None
    client = get_redis_client()
    raw = client.hgetall(f"{MANUAL_STATUS_PREFIX}{case_id}")
    if not raw:
        return None
    return {key: json.loads(value) for key, value in raw.items()}


def record_manual_stats(latency_ms: int, success: bool) -> None:
    if USE_DB_CASE_STORE:
        key_updates = {
            "manual_generation_count": 1,
            "manual_generation_success": 1 if success else 0,
            "manual_generation_failures": 0 if success else 1,
            "manual_generation_latency_ms": int(latency_ms),
        }
        for stat_key, delta in key_updates.items():
            current_raw = _db_get_kv("manual_stats", stat_key)
            current = int(current_raw) if current_raw is not None else 0
            _db_set_kv("manual_stats", stat_key, str(current + int(delta)))
        return
    if USE_MEMORY_STORE:
        _MEM_STATS["manual_generation_count"] = _MEM_STATS.get("manual_generation_count", 0) + 1
        _MEM_STATS["manual_generation_success"] = _MEM_STATS.get("manual_generation_success", 0) + (
            1 if success else 0
        )
        _MEM_STATS["manual_generation_failures"] = _MEM_STATS.get("manual_generation_failures", 0) + (
            0 if success else 1
        )
        _MEM_STATS["manual_generation_latency_ms"] = _MEM_STATS.get("manual_generation_latency_ms", 0) + latency_ms
        return
    client = get_redis_client()
    client.hincrby(MANUAL_STATS_KEY, "manual_generation_count", 1)
    client.hincrby(MANUAL_STATS_KEY, "manual_generation_success", 1 if success else 0)
    client.hincrby(MANUAL_STATS_KEY, "manual_generation_failures", 0 if success else 1)
    client.hincrby(MANUAL_STATS_KEY, "manual_generation_latency_ms", latency_ms)


def get_manual_stats() -> Dict[str, Any]:
    if USE_DB_CASE_STORE:
        stats: Dict[str, Any] = {}
        for stat_key in [
            "manual_generation_count",
            "manual_generation_success",
            "manual_generation_failures",
            "manual_generation_latency_ms",
        ]:
            value = _db_get_kv("manual_stats", stat_key)
            if value is not None:
                stats[stat_key] = int(value)
    elif USE_MEMORY_STORE:
        stats = dict(_MEM_STATS)
    else:
        client = get_redis_client()
        raw = client.hgetall(MANUAL_STATS_KEY)
        stats = {key: int(value) for key, value in raw.items()} if raw else {}
    count = stats.get("manual_generation_count", 0)
    total_ms = stats.get("manual_generation_latency_ms", 0)
    avg_ms = int(total_ms / count) if count else 0
    stats["manual_generation_latency_avg_ms"] = avg_ms
    return stats


def _decode_log_entry(raw: LogRaw) -> Dict[str, Any]:
    now = time.time()
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")

    if isinstance(raw, dict):
        payload: Dict[str, Any] = dict(raw)
    else:
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"line": str(raw)}

    if not isinstance(payload, dict):
        payload = {"line": str(payload)}

    payload.setdefault("ts", now)
    payload.setdefault("stream", "system")
    payload.setdefault("level", "INFO")
    payload.setdefault("line", "")

    line = payload.get("line")
    if isinstance(line, str) and line:
        line = SECRET_PAIR_PATTERN.sub(lambda m: f"{m.group('key')}{m.group('sep')}***", line)
        line = SECRET_JSON_PATTERN.sub(lambda m: f"{m.group('key')}\"***\"", line)
        line = BEARER_PATTERN.sub("Bearer ***", line)
        line = SK_PATTERN.sub("sk-***", line)
        line = JWT_PATTERN.sub("***", line)
        payload["line"] = line

    return payload
