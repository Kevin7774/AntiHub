from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Optional

import jwt

try:
    import bcrypt
except ModuleNotFoundError:  # pragma: no cover - validated in runtime checks
    bcrypt = None  # type: ignore[assignment]


class AuthError(RuntimeError):
    pass


class AuthConfigError(AuthError):
    pass


@dataclass(frozen=True)
class AuthIdentity:
    username: str
    role: str
    tenant_id: str | None = None


@dataclass(frozen=True)
class AuthUserRecord:
    username: str
    password_sha256: str
    role: str


@dataclass(frozen=True)
class AuthBootstrapUser:
    username: str
    role: str
    active: bool
    password: str | None
    password_hash: str | None


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def hash_password(password: str) -> str:
    """
    Legacy SHA-256 helper kept for backward compatibility.

    New persisted credentials should use bcrypt via `hash_password_bcrypt`.
    """

    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _normalize_role(role: str) -> str:
    normalized = str(role or "user").strip().lower()
    if normalized not in {"user", "admin", "root"}:
        return "user"
    return normalized


def _require_bcrypt() -> None:
    if bcrypt is None:
        raise AuthConfigError("bcrypt is required for DB-backed password hashing")


def is_bcrypt_hash(value: str) -> bool:
    raw = str(value or "").strip()
    return any(raw.startswith(prefix) for prefix in _BCRYPT_PREFIXES)


def hash_password_bcrypt(password: str) -> str:
    _require_bcrypt()
    raw = str(password or "")
    if not raw:
        raise AuthConfigError("password cannot be empty")
    hashed = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=12))
    return hashed.decode("utf-8")


def verify_password_hash(password: str, password_hash: str) -> bool:
    secret = str(password_hash or "").strip()
    candidate = str(password or "")
    if not secret:
        return False
    if is_bcrypt_hash(secret):
        _require_bcrypt()
        try:
            return bool(bcrypt.checkpw(candidate.encode("utf-8"), secret.encode("utf-8")))
        except ValueError:
            return False
    if _SHA256_HEX_RE.fullmatch(secret):
        return hmac.compare_digest(hash_password(candidate), secret)
    return False


@lru_cache(maxsize=8)
def load_user_records(users_json: str) -> Dict[str, AuthUserRecord]:
    payload = (users_json or "").strip()
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AuthConfigError(f"AUTH_USERS_JSON is invalid JSON: {exc}") from exc

    records: Dict[str, AuthUserRecord] = {}
    if not isinstance(data, dict):
        raise AuthConfigError("AUTH_USERS_JSON must be an object")

    for username, raw in data.items():
        name = str(username or "").strip()
        if not name:
            continue
        role = "user"
        password_sha256: str | None = None

        if isinstance(raw, str):
            password_sha256 = hash_password(raw)
        elif isinstance(raw, dict):
            role = _normalize_role(str(raw.get("role") or "user"))
            if raw.get("password_sha256"):
                password_sha256 = str(raw.get("password_sha256") or "").strip().lower()
            elif raw.get("password"):
                password_sha256 = hash_password(str(raw.get("password") or ""))
        else:
            continue

        if not password_sha256 or len(password_sha256) != 64:
            continue

        records[name] = AuthUserRecord(username=name, password_sha256=password_sha256, role=role)

    return records


@lru_cache(maxsize=8)
def load_bootstrap_users(users_json: str) -> Dict[str, AuthBootstrapUser]:
    payload = (users_json or "").strip()
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AuthConfigError(f"AUTH_USERS_JSON is invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthConfigError("AUTH_USERS_JSON must be an object")

    users: Dict[str, AuthBootstrapUser] = {}
    for username, raw in data.items():
        name = str(username or "").strip()
        if not name:
            continue
        role = "user"
        active = True
        password_plain: str | None = None
        password_hash_value: str | None = None
        if isinstance(raw, str):
            password_plain = raw
        elif isinstance(raw, dict):
            role = _normalize_role(str(raw.get("role") or "user"))
            active = bool(raw.get("active", True))
            if raw.get("password"):
                password_plain = str(raw.get("password") or "")
            if raw.get("password_hash_bcrypt"):
                password_hash_value = str(raw.get("password_hash_bcrypt") or "").strip()
            elif raw.get("password_hash"):
                password_hash_value = str(raw.get("password_hash") or "").strip()
        if password_plain is None and not password_hash_value:
            continue
        if password_hash_value and not is_bcrypt_hash(password_hash_value):
            # For bootstrap hashes, only accept bcrypt.
            continue
        users[name] = AuthBootstrapUser(
            username=name,
            role=role,
            active=active,
            password=password_plain,
            password_hash=password_hash_value or None,
        )
    return users


def authenticate_user(username: str, password: str, users_json: str) -> Optional[AuthIdentity]:
    records = load_user_records(users_json)
    record = records.get(str(username or ""))
    if not record:
        return None

    candidate_hash = hash_password(str(password or ""))
    if not hmac.compare_digest(record.password_sha256, candidate_hash):
        return None

    return AuthIdentity(username=record.username, role=record.role)


def issue_access_token(identity: AuthIdentity, secret: str, ttl_seconds: int = 3600) -> str:
    if not secret:
        raise AuthConfigError("AUTH_TOKEN_SECRET is missing")
    now = int(time.time())
    payload = {
        "sub": identity.username,
        "role": identity.role,
        "tid": identity.tenant_id,
        "iat": now,
        "exp": now + max(60, int(ttl_seconds)),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> AuthIdentity:
    if not secret:
        raise AuthConfigError("AUTH_TOKEN_SECRET is missing")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid token") from exc

    username = str(payload.get("sub") or "").strip()
    role = _normalize_role(str(payload.get("role") or "user"))
    tenant_id_raw = payload.get("tid")
    tenant_id = str(tenant_id_raw or "").strip() or None
    if not username:
        raise AuthError("invalid token subject")
    return AuthIdentity(username=username, role=role, tenant_id=tenant_id)


def extract_bearer_token(authorization: str | None) -> str:
    raw = str(authorization or "").strip()
    if not raw:
        raise AuthError("missing Authorization header")
    prefix = "Bearer "
    if not raw.startswith(prefix):
        raise AuthError("invalid Authorization header")
    token = raw[len(prefix):].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token
