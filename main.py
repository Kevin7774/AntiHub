from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess  # nosec B404
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import docker
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text

from analyze.report_store import ReportStore, repo_cache_key
from auth import (
    AuthBootstrapUser,
    AuthError,
    AuthIdentity,
    authenticate_user,
    decode_access_token,
    extract_bearer_token,
    hash_password_bcrypt,
    is_bcrypt_hash,
    issue_access_token,
    load_bootstrap_users,
    verify_password_hash,
)
from billing import (
    ENGINE as BILLING_ENGINE,
)
from billing import (
    BillingRateLimiter,
    BillingRepository,
    BillingStateError,
    PaymentWebhookError,
    get_user_entitlements,
    get_payment_provider,
    init_billing_db,
    invalidate_plan_entitlements,
    invalidate_user_entitlements,
    process_payment_webhook,
    resolve_user_rpm,
    require_entitlement,
    session_scope,
    verify_webhook_signature,
)
from billing.wechatpay import (
    decrypt_notification,
    parse_platform_certs,
    verify_wechatpay_notify_signature,
)
from config import (
    ANALYZE_LOCK_TTL_SECONDS,
    API_HOST,
    API_PORT,
    APP_ENV,
    APP_VERSION,
    AUTH_ENABLED,
    AUTH_TOKEN_SECRET,
    AUTH_TOKEN_TTL_SECONDS,
    AUTH_USERS_JSON,
    CASE_LABEL_KEY,
    CASE_LABEL_MANAGED,
    CORS_ORIGINS,
    DEEP_SEARCH_POINTS_COST,
    DISABLE_RECOMMEND_RATE_LIMIT,
    FEATURE_SAAS_ADMIN_API,
    FEATURE_SAAS_ENTITLEMENTS,
    GIT_SHA,
    ONE_CLICK_DEPLOY_POINTS_COST,
    OPENCLAW_BASE_URL,
    PAYMENT_PROVIDER,
    PAYMENT_WEBHOOK_SECRET,
    PLANS,
    PORT_MODE,
    PUBLIC_HOST,
    RECOMMEND_MAX_UPLOAD_BYTES,
    REDIS_DISABLED,
    ROOT_ADMIN_FORCE_SYNC,
    ROOT_ADMIN_PASSWORD,
    ROOT_ADMIN_PASSWORD_HASH,
    ROOT_ADMIN_USERNAME,
    ROOT_PATH,
    STARTUP_BOOTSTRAP_ENABLED,
    STARTUP_TIMEOUT_SECONDS,
    VISUAL_LOCK_TTL_SECONDS,
    VISUAL_VIDEO_ENABLED,
    WECHATPAY_API_BASE_URL,
    WECHATPAY_APIV3_KEY,
    WECHATPAY_APPID,
    WECHATPAY_CERT_SERIAL,
    WECHATPAY_MCHID,
    WECHATPAY_NOTIFY_URL,
    WECHATPAY_PLATFORM_CERT_PATH,
    WECHATPAY_PLATFORM_CERT_PEM,
    WECHATPAY_PLATFORM_CERT_SERIAL,
    WECHATPAY_PLATFORM_CERTS_JSON,
    WECHATPAY_PRIVATE_KEY_PATH,
    WECHATPAY_PRIVATE_KEY_PEM,
)
from decision import (
    init_decision_db,
    recommend_products,
    resolve_product_action,
    seed_default_catalog,
)
from docker_ops import wait_for_container_running
from errors import ERROR_CODE_MAP
from observability import configure_json_logging, get_logger, log_event
from recommend.models import RecommendationResponse
from recommend.service import is_deep_search_mode
from recommend.text_extract import extract_text_from_upload
from runtime_metrics import get_runtime_metrics_snapshot, record_request_metric
from storage import (
    acquire_analyze_lock,
    acquire_visualize_lock,
    append_log,
    decode_log_entry,
    get_async_redis_client,
    get_case,
    get_logs,
    get_logs_slice,
    get_manual,
    get_manual_stats,
    get_manual_status,
    get_redis_client,
    list_case_ids,
    log_channel,
    set_case,
    set_manual_status,
    update_case,
)
from templates_store import get_template, load_templates
from visualize.store import VisualStore, visual_cache_key, visuals_dir
from worker import (
    BuildError,
    analyze_case,
    build_and_run,
    generate_manual_task,
    release_port,
    reserve_specific_port,
    visualize_case,
)

PLAN_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_\-:.]{0,127}$"
EXTERNAL_ORDER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_\-:.]{0,127}$"
CURRENCY_PATTERN = r"^[A-Za-z]{3}$"
TENANT_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$"
ENTITLEMENT_KEY_PATTERN = r"^[a-z][a-z0-9_:.\\-]{1,127}$"
MALICIOUS_XSS_PATTERN = re.compile(r"(?is)<\s*/?\s*script\b|javascript:|on\w+\s*=")
MALICIOUS_SQLI_PATTERN = re.compile(
    r"(?is)\bunion\b\s+\bselect\b|\bdrop\b\s+\btable\b|\bdelete\b\s+\bfrom\b|\binsert\b\s+\binto\b|\bor\b\s+1\s*=\s*1\b|\band\b\s+1\s*=\s*1\b"
)

SECURITY_HEADERS: Dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https:; "
    "connect-src 'self' ws: wss: http: https:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

configure_json_logging(level=logging.INFO)
APP_LOGGER = get_logger("antihub.api")


def _normalize_role_value(raw_role: str) -> str:
    role = str(raw_role or "user").strip().lower()
    if role not in {"user", "admin", "root"}:
        return "user"
    return role


_ROLE_WEIGHT: dict[str, int] = {"user": 10, "admin": 50, "root": 100}


def _has_min_role(identity: AuthIdentity, required_role: str) -> bool:
    expected = _ROLE_WEIGHT.get(_normalize_role_value(required_role), _ROLE_WEIGHT["user"])
    actual = _ROLE_WEIGHT.get(_normalize_role_value(identity.role), _ROLE_WEIGHT["user"])
    return actual >= expected


def _is_admin_role(identity: AuthIdentity) -> bool:
    return _has_min_role(identity, "admin")


def _is_root_role(identity: AuthIdentity) -> bool:
    return _normalize_role_value(identity.role) == "root"


def _slugify_tenant_code(raw: str) -> str:
    normalized = str(raw or "").strip().lower()
    if not normalized:
        return ""
    normalized = re.sub(r"[^a-z0-9_-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-_")
    return normalized[:64]


def _derive_tenant_code(repo: BillingRepository, tenant_name: str, fallback: str) -> str:
    base = _slugify_tenant_code(tenant_name) or _slugify_tenant_code(fallback) or "tenant"
    candidate = base[:64]
    if not repo.get_tenant_by_code(candidate):
        return candidate
    for _ in range(32):
        suffix = uuid.uuid4().hex[:6]
        next_candidate = f"{base[:57]}-{suffix}".strip("-")
        if not repo.get_tenant_by_code(next_candidate):
            return next_candidate
    return f"tenant-{uuid.uuid4().hex[:8]}"


def _to_auth_user_info(identity: AuthIdentity, repo: BillingRepository) -> "AuthUserInfo":
    user = repo.get_auth_user(identity.username)
    if user is None:
        return AuthUserInfo(username=identity.username, role=identity.role)
    tenant = user.tenant if user.tenant_id else None
    if tenant is None and user.tenant_id:
        tenant = repo.get_tenant_by_id(str(user.tenant_id))
    return AuthUserInfo(
        username=str(user.username),
        role=_normalize_role_value(str(user.role)),
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        tenant_code=str(tenant.code) if tenant else None,
        tenant_name=str(tenant.name) if tenant else None,
    )


def _scopes_for_identity(identity: AuthIdentity) -> list[str]:
    role = _normalize_role_value(identity.role)
    base_scopes = [
        "self:read",
        "workspace:read",
        "billing:read_self",
    ]
    if role in {"admin", "root"}:
        base_scopes.extend(
            [
                "tenant:read",
                "tenant:user:read",
                "tenant:user:write",
                "billing:admin_read",
                "billing:admin_write",
            ]
        )
    if role == "root":
        base_scopes.extend(
            [
                "tenant:write_global",
                "tenant:user:write_global",
                "iam:root",
            ]
        )
    return sorted(set(base_scopes))


def _upsert_bootstrap_user(repo: BillingRepository, user: AuthBootstrapUser) -> None:
    if user.password_hash:
        password_hash = str(user.password_hash).strip()
    elif user.password is not None:
        password_hash = hash_password_bcrypt(str(user.password))
    else:
        return
    role = _normalize_role_value(user.role)
    tenant_id: str | None = None
    if role != "root":
        default_tenant = repo.get_or_create_tenant(code="default", name="Default Tenant", active=True)
        tenant_id = str(default_tenant.id)
    repo.upsert_auth_user(
        username=user.username,
        password_hash=password_hash,
        role=role,
        active=bool(user.active),
        tenant_id=tenant_id,
    )


def _bootstrap_auth_users_from_config() -> None:
    if not AUTH_ENABLED:
        return
    if not AUTH_USERS_JSON:
        return
    try:
        users = load_bootstrap_users(AUTH_USERS_JSON)
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.bootstrap.config_invalid", error=str(exc))
        return
    if not users:
        return
    try:
        with session_scope() as session:
            if session is None:
                return
            repo = BillingRepository(session)
            existing = repo.count_auth_users()
            if existing > 0:
                return
            inserted = 0
            for user in users.values():
                _upsert_bootstrap_user(repo, user)
                inserted += 1
            log_event(APP_LOGGER, logging.INFO, "auth.bootstrap.seeded", inserted=inserted)
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.bootstrap.db_unavailable", error=str(exc))


def _resolve_root_password_hash() -> str | None:
    hashed = str(ROOT_ADMIN_PASSWORD_HASH or "").strip()
    if hashed:
        if not is_bcrypt_hash(hashed):
            log_event(APP_LOGGER, logging.WARNING, "auth.root.bootstrap.invalid_hash")
            return None
        return hashed
    plain = str(ROOT_ADMIN_PASSWORD or "")
    if not plain:
        return None
    try:
        return hash_password_bcrypt(plain)
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.root.bootstrap.hash_failed", error=str(exc))
        return None


def _bootstrap_root_admin_user() -> None:
    if not AUTH_ENABLED:
        return
    username = str(ROOT_ADMIN_USERNAME or "").strip()
    if not username:
        return
    password_hash = _resolve_root_password_hash()
    try:
        with session_scope() as session:
            if session is None:
                return
            repo = BillingRepository(session)
            existing = repo.get_auth_user(username)
            if existing is None:
                if not password_hash:
                    log_event(APP_LOGGER, logging.WARNING, "auth.root.bootstrap.skipped", reason="missing_password")
                    return
                repo.create_auth_user(
                    username=username,
                    password_hash=password_hash,
                    role="root",
                    active=True,
                    tenant_id=None,
                )
                log_event(APP_LOGGER, logging.INFO, "auth.root.bootstrap.created", username=username)
                return

            patch_password = False
            next_password_hash = str(getattr(existing, "password_hash", "") or "")
            if ROOT_ADMIN_FORCE_SYNC and password_hash:
                patch_password = True
                next_password_hash = password_hash
            repo.update_auth_user(
                username=username,
                role="root",
                active=True,
                password_hash=next_password_hash if patch_password else None,
                tenant_id="",
            )
            log_event(APP_LOGGER, logging.INFO, "auth.root.bootstrap.synced", username=username)
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.root.bootstrap.failed", username=username, error=str(exc))


def _authenticate_user_from_db(username: str, password: str) -> AuthIdentity | None:
    try:
        with session_scope() as session:
            if session is None:
                return None
            repo = BillingRepository(session)
            user = repo.get_auth_user(username)
            if user is None or not bool(getattr(user, "active", True)):
                return None
            tenant = user.tenant if getattr(user, "tenant_id", None) else None
            if tenant is None and getattr(user, "tenant_id", None):
                tenant = repo.get_tenant_by_id(str(getattr(user, "tenant_id")))
            if tenant is not None and not bool(getattr(tenant, "active", True)):
                return None
            if not verify_password_hash(password, str(getattr(user, "password_hash", "") or "")):
                return None
            repo.touch_auth_user_login(str(user.username))
            return AuthIdentity(
                username=str(user.username),
                role=_normalize_role_value(str(user.role)),
                tenant_id=str(getattr(user, "tenant_id", "") or "").strip() or None,
            )
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.db.lookup_failed", username=username, error=str(exc))
        return None


def _migrate_legacy_user_to_db(identity: AuthIdentity, password: str) -> None:
    try:
        password_hash = hash_password_bcrypt(password)
        with session_scope() as session:
            if session is None:
                return
            repo = BillingRepository(session)
            role = _normalize_role_value(identity.role)
            tenant_id = identity.tenant_id
            if role != "root" and not tenant_id:
                default_tenant = repo.get_or_create_tenant(code="default", name="Default Tenant", active=True)
                tenant_id = str(default_tenant.id)
            repo.upsert_auth_user(
                username=identity.username,
                password_hash=password_hash,
                role=role,
                active=True,
                tenant_id=tenant_id,
            )
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.migrate_legacy_user_failed", username=identity.username, error=str(exc))


@asynccontextmanager
async def lifespan(_: FastAPI):
    if AUTH_ENABLED:
        if not AUTH_TOKEN_SECRET:
            raise RuntimeError("AUTH_TOKEN_SECRET is required when AUTH_ENABLED=true")
    if STARTUP_BOOTSTRAP_ENABLED:
        init_billing_db()
        init_decision_db()
        _bootstrap_root_admin_user()
        _bootstrap_auth_users_from_config()
        seed_default_catalog()
    yield


app = FastAPI(title="Agent Platform", version=APP_VERSION, root_path=ROOT_PATH, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Signature",
        "X-Webhook-Signature",
        "X-Wechatpay-Timestamp",
        "X-Wechatpay-Nonce",
        "X-Wechatpay-Signature",
        "X-Wechatpay-Serial",
    ],
    expose_headers=["X-Trace-Id", "X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After"],
)


PUBLIC_AUTH_PATHS = {
    "/health",
    "/healthz",
    "/health/billing",
    "/auth/login",
    "/auth/register",
    "/billing/webhooks/payment",
    "/billing/webhooks/wechatpay",
    "/docs",
    "/redoc",
    "/openapi.json",
}

BILLING_RATE_LIMITER = BillingRateLimiter()


def _contains_malicious_payload(value: str) -> bool:
    text = str(value or "")
    if not text:
        return False
    return bool(MALICIOUS_XSS_PATTERN.search(text) or MALICIOUS_SQLI_PATTERN.search(text))


def _require_safe_input(field_name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if normalized and _contains_malicious_payload(normalized):
        raise ValueError(f"{field_name} contains disallowed payload")
    return normalized


def _normalize_request_path(path: str) -> str:
    normalized = path or "/"
    if ROOT_PATH and normalized.startswith(ROOT_PATH):
        stripped = normalized[len(ROOT_PATH):]
        normalized = stripped if stripped.startswith("/") else f"/{stripped}"
        if not normalized or normalized == "":
            normalized = "/"
    return normalized


def _is_public_path(path: str) -> bool:
    path = _normalize_request_path(path)
    # ✅ auth endpoints must be public
    if path in {"/login", "/register", "/openapi.json", "/docs", "/redoc"}:
        return True
    normalized = _normalize_request_path(path)
    if normalized in PUBLIC_AUTH_PATHS:
        return True
    return normalized.startswith("/docs/") or normalized.startswith("/redoc/")


def _should_rate_limit_recommendation(request: Request) -> bool:
    if request.method.upper() != "POST":
        return False
    normalized = _normalize_request_path(request.url.path)
    return normalized in {"/recommendations", "/recommendations/stream"}


def _get_identity_for_rate_limit(request: Request) -> AuthIdentity | None:
    identity = getattr(request.state, "auth_identity", None)
    if isinstance(identity, AuthIdentity):
        return identity
    if not AUTH_ENABLED:
        return AuthIdentity(username="anonymous", role="admin")
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None
    try:
        token = extract_bearer_token(authorization)
        decoded = decode_access_token(token, AUTH_TOKEN_SECRET)
        with session_scope() as session:
            if session is None:
                return decoded
            repo = BillingRepository(session)
            user = repo.get_auth_user(decoded.username)
            if user is None:
                return decoded
            if not bool(getattr(user, "active", True)):
                return None
            tenant_id = str(getattr(user, "tenant_id", "") or "").strip() or None
            return AuthIdentity(
                username=str(user.username),
                role=_normalize_role_value(str(user.role)),
                tenant_id=tenant_id,
            )
    except AuthError:
        return None
    except Exception:
        return None


def _request_user_id(request: Request) -> str:
    identity = getattr(request.state, "auth_identity", None)
    if isinstance(identity, AuthIdentity):
        return str(identity.username or "anonymous")
    return "anonymous"


def _request_trace_id(request: Request) -> str:
    raw = str(getattr(request.state, "trace_id", "") or "").strip()
    if raw:
        return raw
    return uuid.uuid4().hex


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    trace_id = str(request.headers.get("X-Trace-Id") or uuid.uuid4().hex).strip()[:64]
    request.state.trace_id = trace_id
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = int((time.perf_counter() - started) * 1000)
    user_id = _request_user_id(request)
    log_event(
        APP_LOGGER,
        logging.INFO,
        "request.completed",
        trace_id=trace_id,
        method=request.method,
        path=_normalize_request_path(request.url.path),
        status_code=response.status_code,
        duration_ms=duration_ms,
        user_id=user_id,
    )
    record_request_metric(
        path=_normalize_request_path(request.url.path),
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        if key not in response.headers:
            response.headers[key] = value
    path = _normalize_request_path(request.url.path)
    if path.startswith("/docs") or path.startswith("/redoc"):
        return response
    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not AUTH_ENABLED or request.method.upper() == "OPTIONS" or _is_public_path(request.url.path):
        return await call_next(request)
    try:
        authorization = request.headers.get("Authorization")
        if authorization:
            token = extract_bearer_token(authorization)
        else:
            token: str | None = None
            if request.method.upper() in {"GET", "HEAD"}:
                token = str(request.query_params.get("token") or "").strip() or None
            if not token:
                raise AuthError("missing Authorization header")
        identity = decode_access_token(token, AUTH_TOKEN_SECRET)
        with session_scope() as session:
            if session is not None:
                repo = BillingRepository(session)
                user = repo.get_auth_user(identity.username)
                if user is not None:
                    if not bool(getattr(user, "active", True)):
                        raise AuthError("inactive account")
                    tenant_id = str(getattr(user, "tenant_id", "") or "").strip() or None
                    tenant = user.tenant if tenant_id else None
                    if tenant is None and tenant_id:
                        tenant = repo.get_tenant_by_id(tenant_id)
                    if tenant is not None and not bool(getattr(tenant, "active", True)):
                        raise AuthError("tenant is inactive")
                    identity = AuthIdentity(
                        username=str(user.username),
                        role=_normalize_role_value(str(user.role)),
                        tenant_id=tenant_id,
                    )
    except AuthError as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})
    request.state.auth_identity = identity
    return await call_next(request)


@app.middleware("http")
async def recommendation_rate_limit_middleware(request: Request, call_next):
    if DISABLE_RECOMMEND_RATE_LIMIT:
        return await call_next(request)
    if not _should_rate_limit_recommendation(request):
        return await call_next(request)

    identity = _get_identity_for_rate_limit(request)
    if identity is None:
        return await call_next(request)

    limit_rpm = resolve_user_rpm(identity.username)
    try:
        verdict = BILLING_RATE_LIMITER.allow(
            subject=f"user:{identity.username}:recommendations",
            limit_rpm=limit_rpm,
        )
    except RuntimeError as exc:
        trace_id = _request_trace_id(request)
        log_event(
            APP_LOGGER,
            logging.ERROR,
            "rate_limit.unavailable",
            trace_id=trace_id,
            user_id=identity.username,
            error=str(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "rate limiter unavailable"},
            headers={"X-Trace-Id": trace_id},
        )
    if not verdict.allowed:
        return JSONResponse(
            status_code=429,
            content={
                "detail": "rate limit exceeded",
                "limit_rpm": verdict.limit_rpm,
                "retry_after_seconds": verdict.retry_after_seconds,
            },
            headers={
                "Retry-After": str(verdict.retry_after_seconds),
                "X-RateLimit-Limit": str(verdict.limit_rpm),
                "X-RateLimit-Remaining": str(verdict.remaining),
            },
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(verdict.limit_rpm)
    response.headers["X-RateLimit-Remaining"] = str(verdict.remaining)
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = _request_trace_id(request)
    user_id = _request_user_id(request)
    log_event(
        APP_LOGGER,
        logging.ERROR,
        "request.unhandled_exception",
        trace_id=trace_id,
        user_id=user_id,
        method=request.method,
        path=_normalize_request_path(request.url.path),
        exception_type=type(exc).__name__,
        exception_message=str(exc),
    )
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_SERVER_ERROR",
            "message": "internal server error",
            "trace_id": trace_id,
        },
        headers={"X-Trace-Id": trace_id},
    )


def get_current_identity(request: Request) -> AuthIdentity:
    identity = getattr(request.state, "auth_identity", None)
    if isinstance(identity, AuthIdentity):
        return identity
    raise HTTPException(status_code=401, detail="unauthorized")


def require_role(required_role: str):
    normalized_required = _normalize_role_value(required_role)

    def _dependency(identity: AuthIdentity = Depends(get_current_identity)) -> AuthIdentity:
        if not _has_min_role(identity, normalized_required):
            raise HTTPException(status_code=403, detail=f"{normalized_required} role required")
        return identity

    return _dependency


def require_admin(identity: AuthIdentity = Depends(get_current_identity)) -> AuthIdentity:
    if not _is_admin_role(identity):
        raise HTTPException(status_code=403, detail="admin role required")
    return identity


def require_root(identity: AuthIdentity = Depends(get_current_identity)) -> AuthIdentity:
    if not _is_root_role(identity):
        raise HTTPException(status_code=403, detail="root role required")
    return identity


def _ensure_feature_enabled(enabled: bool) -> None:
    if not enabled:
        raise HTTPException(status_code=404, detail="feature disabled")


def _identity_tenant_id(repo: BillingRepository, identity: AuthIdentity) -> str | None:
    claimed = str(identity.tenant_id or "").strip() or None
    user = repo.get_auth_user(identity.username)
    if user is None:
        return claimed
    db_tenant = str(getattr(user, "tenant_id", "") or "").strip() or None
    return db_tenant or claimed


def _enforce_tenant_scope(repo: BillingRepository, identity: AuthIdentity, tenant_id: str) -> None:
    target = str(tenant_id or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    if _is_root_role(identity):
        return
    if not _is_admin_role(identity):
        raise HTTPException(status_code=403, detail="admin role required")
    actor_tenant_id = _identity_tenant_id(repo, identity)
    if not actor_tenant_id:
        raise HTTPException(status_code=403, detail="tenant scoped access denied")
    if str(actor_tenant_id) != target:
        raise HTTPException(status_code=403, detail="cross-tenant access denied")


def _enforce_user_management_scope(
    repo: BillingRepository,
    identity: AuthIdentity,
    *,
    target_user: Any,
    intended_role: str | None = None,
    intended_tenant_id: str | None = None,
) -> None:
    if _is_root_role(identity):
        return
    if not _is_admin_role(identity):
        raise HTTPException(status_code=403, detail="admin role required")

    actor_tenant_id = _identity_tenant_id(repo, identity)
    target_tenant_id = str(getattr(target_user, "tenant_id", "") or "").strip() or None
    if not actor_tenant_id or not target_tenant_id or str(actor_tenant_id) != str(target_tenant_id):
        raise HTTPException(status_code=403, detail="cross-tenant user management denied")

    current_role = _normalize_role_value(str(getattr(target_user, "role", "user")))
    if current_role in {"admin", "root"} and str(getattr(target_user, "username", "")) != identity.username:
        raise HTTPException(status_code=403, detail="cannot manage peer admin/root user")

    if intended_role is not None and _normalize_role_value(intended_role) != "user":
        raise HTTPException(status_code=403, detail="admin can only assign user role")

    if intended_tenant_id is not None:
        next_tenant = str(intended_tenant_id or "").strip() or None
        if next_tenant and str(next_tenant) != str(actor_tenant_id):
            raise HTTPException(status_code=403, detail="admin cannot move user to another tenant")


def _tenant_usernames(repo: BillingRepository, tenant_id: str) -> set[str]:
    users = repo.list_auth_users(
        include_inactive=True,
        tenant_id=str(tenant_id).strip(),
        limit=500,
        offset=0,
    )
    return {str(user.username) for user in users if str(user.username)}


async def authenticate_websocket(websocket: WebSocket) -> AuthIdentity:
    if not AUTH_ENABLED:
        return AuthIdentity(username="anonymous", role="admin")
    token = websocket.query_params.get("token")
    if token:
        return decode_access_token(token, AUTH_TOKEN_SECRET)
    return decode_access_token(extract_bearer_token(websocket.headers.get("Authorization")), AUTH_TOKEN_SECRET)

STAGE_VALUES = {"clone", "build", "run", "analyze", "system", "showcase"}
STATUS_STAGE_MAP = {
    "PENDING": "system",
    "CLONING": "clone",
    "BUILDING": "build",
    "STARTING": "run",
    "RUNNING": "run",
    "STOPPED": "run",
    "FINISHED": "run",
    "FAILED": "system",
    "ANALYZING": "analyze",
    "ARCHIVED": "system",
    "SHOWCASE_READY": "showcase",
    "SHOWCASE_FAILED": "showcase",
}

EXPECTED_STATUS_VALUES = set(STATUS_STAGE_MAP.keys())
EXPECTED_ERROR_ALIASES = {
    "BUILD_FAILED": "DOCKER_BUILD_FAILED",
    "DOCKER_BUILD_FAIL": "DOCKER_BUILD_FAILED",
    "BUILD_KIT_REQUIRED": "BUILDKIT_REQUIRED",
    "BUILDKIT": "BUILDKIT_REQUIRED",
}


class BuildOptions(BaseModel):
    network: Optional[str] = Field(
        None,
        description="Docker build network mode (bridge/host).",
    )
    no_cache: Optional[bool] = Field(
        None,
        description="Disable docker build cache.",
    )
    use_buildkit: Optional[bool] = Field(
        None,
        description="Enable BuildKit for docker build.",
    )
    build_args: Dict[str, str] = Field(
        default_factory=dict,
        description="Docker build arguments (values are not stored).",
    )

    model_config = ConfigDict(extra="ignore")


class GitOptions(BaseModel):
    enable_submodule: Optional[bool] = Field(
        None,
        description="Enable git submodule update after clone.",
    )
    enable_lfs: Optional[bool] = Field(
        None,
        description="Enable git lfs pull after clone.",
    )

    model_config = ConfigDict(extra="ignore")


class CaseCreateRequest(BaseModel):
    template_id: Optional[str] = Field(
        None,
        description="Optional template id to prefill repo/ref/mode/env.",
    )
    git_url: Optional[str] = Field(
        None,
        description="Git repository URL (alias for repo_url).",
    )
    repo_url: Optional[str] = Field(
        None,
        description="Git repository URL.",
    )
    ref: Optional[str] = Field(
        None,
        description="Git ref (alias for branch). Optional; empty or 'auto' to auto-detect default branch.",
    )
    branch: Optional[str] = Field(
        None,
        description="Git branch (alias for ref). Optional; empty or 'auto' to auto-detect default branch.",
    )
    mode: Optional[str] = Field(
        None,
        description="(Legacy) Case mode: deploy/showcase. Prefer run_mode.",
    )
    run_mode: Optional[str] = Field(
        None,
        description="Run mode: auto/container/showcase. Default is auto.",
    )
    auto_mode: Optional[bool] = Field(
        False,
        description="Auto downgrade to showcase if Dockerfile is missing.",
    )
    auto_manual: Optional[bool] = Field(
        True,
        description="Auto generate manual after clone.",
    )
    container_port: Optional[int] = Field(None, description="Container port override")
    dockerfile_path: Optional[str] = Field(
        "Dockerfile",
        description="Optional Dockerfile path relative to repo root.",
    )
    compose_file: Optional[str] = Field(
        None,
        description="Optional docker compose file path relative to repo root.",
    )
    context_path: Optional[str] = Field(
        ".",
        description="Optional build context path relative to repo root.",
    )
    docker_build_network: Optional[str] = Field(
        None,
        description="Docker build network mode (bridge/host).",
    )
    docker_no_cache: Optional[bool] = Field(
        None,
        description="Disable docker build cache.",
    )
    docker_build_args: Dict[str, str] = Field(
        default_factory=dict,
        description="Docker build arguments (values are not stored).",
    )
    build: Optional[BuildOptions] = Field(
        None,
        description="Optional build settings override.",
    )
    enable_submodules: Optional[bool] = Field(
        None,
        description="Enable git submodule update after clone.",
    )
    enable_submodule: Optional[bool] = Field(
        None,
        description="Alias for enable_submodules.",
    )
    enable_lfs: Optional[bool] = Field(
        None,
        description="Enable git lfs pull after clone.",
    )
    git: Optional[GitOptions] = Field(
        None,
        description="Optional git settings override.",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables (values are not stored).",
    )
    auto_analyze: bool = Field(False, description="Trigger analysis after deploy")
    one_click_deploy: bool = Field(
        False,
        description="When true, treat this create request as paid one-click deployment.",
    )
    tenant_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=64,
        description="Optional target tenant id (root only).",
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def _validate_safe_strings(self) -> "CaseCreateRequest":
        for field_name in [
            "template_id",
            "git_url",
            "repo_url",
            "ref",
            "branch",
            "dockerfile_path",
            "compose_file",
            "context_path",
            "docker_build_network",
        ]:
            value = getattr(self, field_name, None)
            if isinstance(value, str):
                _require_safe_input(field_name, value)
        return self

    @field_validator("tenant_id", mode="before")
    @classmethod
    def _normalize_tenant_id(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value or "").strip()
        return normalized or None


class CaseActionRequest(BaseModel):
    action: str = Field(..., description="stop | restart | retry | archive")
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional env override for retry (values are not stored).",
    )


class CaseRetryRequest(BaseModel):
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional env override for retry (values are not stored).",
    )
    docker_build_args: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional build args override for retry (values are not stored).",
    )


class RuntimeInfo(BaseModel):
    container_id: Optional[str] = None
    host_port: Optional[int] = None
    access_url: Optional[str] = None
    url: Optional[str] = None
    started_at: Optional[float] = None
    exited_at: Optional[float] = None
    exit_code: Optional[int] = None
    ports: List[int] = Field(default_factory=list)
    services: List[str] = Field(default_factory=list)


class PreflightMeta(BaseModel):
    stages: List[str] = Field(default_factory=list)
    external_images_to_pull: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    dockerfile_path: Optional[str] = None
    compose_file: Optional[str] = None
    context_path: Optional[str] = None
    candidates: List[str] = Field(default_factory=list)


class ManualMeta(BaseModel):
    generated_at: float
    generator_version: str
    repo_fingerprint: Optional[str] = None
    similarity_score: Optional[float] = None
    warnings: List[str] = Field(default_factory=list)
    signals: Dict[str, Any]
    time_cost_ms: int


class TemplateInfo(BaseModel):
    template_id: str
    name: str
    group: Optional[str] = None
    description: Optional[str] = None
    repo_url: str
    dockerfile_path: Optional[str] = None
    context_path: Optional[str] = None
    default_mode: str = "deploy"
    default_ref: str = "auto"
    suggested_env_keys: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    dimensions: List[str] = Field(default_factory=list)
    expected: Optional[TemplateExpected] = None
    what_to_verify: Optional[str] = None


class PlanInfo(BaseModel):
    plan_id: str
    name: str
    description: Optional[str] = None
    limits: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None


class UsageInfo(BaseModel):
    date: str
    total_cases: int
    running_cases: int
    today_cases: int


class AuthLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)

    @field_validator("username", mode="before")
    @classmethod
    def _sanitize_username(cls, value: Any) -> str:
        return _require_safe_input("username", str(value or ""))

    @field_validator("password", mode="before")
    @classmethod
    def _normalize_password(cls, value: Any) -> str:
        return str(value or "")


class AuthRegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=8, max_length=256)
    tenant_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    tenant_code: Optional[str] = Field(default=None, min_length=2, max_length=64, pattern=TENANT_CODE_PATTERN)

    @field_validator("username", mode="before")
    @classmethod
    def _sanitize_username(cls, value: Any) -> str:
        return _require_safe_input("username", str(value or ""))

    @field_validator("password", mode="before")
    @classmethod
    def _normalize_password(cls, value: Any) -> str:
        return str(value or "")

    @field_validator("tenant_name", mode="before")
    @classmethod
    def _sanitize_tenant_name(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        cleaned = _require_safe_input("tenant_name", str(value or "")).strip()
        return cleaned or None

    @field_validator("tenant_code", mode="before")
    @classmethod
    def _normalize_tenant_code(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        return raw


class AuthUserInfo(BaseModel):
    username: str
    role: str
    tenant_id: Optional[str] = None
    tenant_code: Optional[str] = None
    tenant_name: Optional[str] = None


class AuthPermissionSnapshot(BaseModel):
    username: str
    role: str
    tenant_id: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)


class AuthLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserInfo


class TenantInfo(BaseModel):
    tenant_id: str
    code: str
    name: str
    active: bool


class TenantCreateRequest(BaseModel):
    code: Optional[str] = Field(default=None, min_length=2, max_length=64, pattern=TENANT_CODE_PATTERN)
    name: str = Field(..., min_length=1, max_length=120)
    active: bool = True

    @field_validator("code", mode="before")
    @classmethod
    def _normalize_code(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        normalized = _slugify_tenant_code(str(value or ""))
        return normalized or None

    @field_validator("name", mode="before")
    @classmethod
    def _sanitize_name(cls, value: Any) -> str:
        return _require_safe_input("name", str(value or "")).strip()


class TenantUpdateRequest(BaseModel):
    code: Optional[str] = Field(default=None, min_length=2, max_length=64, pattern=TENANT_CODE_PATTERN)
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    active: Optional[bool] = None

    @field_validator("code", mode="before")
    @classmethod
    def _normalize_code(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        normalized = _slugify_tenant_code(str(value or ""))
        return normalized or None

    @field_validator("name", mode="before")
    @classmethod
    def _sanitize_name(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        cleaned = _require_safe_input("name", str(value or "")).strip()
        return cleaned or None


class OrgUserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field(default="user", min_length=1, max_length=16)
    tenant_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    active: bool = True

    @field_validator("username", mode="before")
    @classmethod
    def _sanitize_username(cls, value: Any) -> str:
        return _require_safe_input("username", str(value or ""))

    @field_validator("password", mode="before")
    @classmethod
    def _normalize_password(cls, value: Any) -> str:
        return str(value or "")

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, value: Any) -> str:
        return _normalize_role_value(str(value or "user"))

    @field_validator("tenant_id", mode="before")
    @classmethod
    def _normalize_tenant_id(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value or "").strip() or None


class OrgUserUpdateRequest(BaseModel):
    password: Optional[str] = Field(default=None, min_length=8, max_length=256)
    role: Optional[str] = Field(default=None, min_length=1, max_length=16)
    tenant_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    active: Optional[bool] = None

    @field_validator("password", mode="before")
    @classmethod
    def _normalize_password(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value or "")

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return _normalize_role_value(str(value or "user"))

    @field_validator("tenant_id", mode="before")
    @classmethod
    def _normalize_tenant_id(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value or "").strip() or None


class BillingPlanResponse(BaseModel):
    plan_id: str
    code: str
    name: str
    description: Optional[str] = None
    currency: str
    price_cents: int
    monthly_points: int
    active: bool


class SaaSPlanResponse(BillingPlanResponse):
    billing_cycle: Optional[str] = None
    trial_days: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BillingPlanCreateRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, pattern=PLAN_CODE_PATTERN)
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    currency: str = Field(default="usd", min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    price_cents: int = Field(0, ge=0, le=100_000_000_00)
    monthly_points: int = Field(0, ge=0, le=100_000_000)
    billing_cycle: str = Field(default="monthly", min_length=1, max_length=16)
    trial_days: int = Field(default=0, ge=0, le=3650)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    active: bool = True

    @field_validator("code", mode="before")
    @classmethod
    def _strip_code(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> str:
        return _require_safe_input("name", str(value or ""))

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> str:
        return str(value or "").strip().lower()

    @field_validator("description", mode="before")
    @classmethod
    def _sanitize_description(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        normalized = _require_safe_input("description", str(value))
        return normalized or None

    @field_validator("billing_cycle", mode="before")
    @classmethod
    def _normalize_billing_cycle(cls, value: Any) -> str:
        return str(value or "monthly").strip().lower() or "monthly"

    @field_validator("metadata", mode="before")
    @classmethod
    def _normalize_metadata(cls, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return dict(value)


class BillingPlanUpdateRequest(BaseModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern=PLAN_CODE_PATTERN)
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3, pattern=CURRENCY_PATTERN)
    price_cents: Optional[int] = Field(default=None, ge=0, le=100_000_000_00)
    monthly_points: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    billing_cycle: Optional[str] = Field(default=None, min_length=1, max_length=16)
    trial_days: Optional[int] = Field(default=None, ge=0, le=3650)
    metadata: Optional[Dict[str, Any]] = None
    active: Optional[bool] = None

    @field_validator("code", mode="before")
    @classmethod
    def _strip_code(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return _require_safe_input("name", str(value))

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value).strip().lower()

    @field_validator("description", mode="before")
    @classmethod
    def _sanitize_description(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        normalized = _require_safe_input("description", str(value))
        return normalized or None

    @field_validator("billing_cycle", mode="before")
    @classmethod
    def _normalize_billing_cycle(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value or "").strip().lower() or None

    @field_validator("metadata", mode="before")
    @classmethod
    def _normalize_metadata(cls, value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return dict(value)


class BillingPlanEntitlementResponse(BaseModel):
    entitlement_id: str
    plan_id: str
    key: str
    enabled: bool
    value: Any = None
    limit: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BillingPlanEntitlementCreateRequest(BaseModel):
    key: str = Field(..., min_length=2, max_length=128, pattern=ENTITLEMENT_KEY_PATTERN)
    enabled: bool = True
    value: Any = None
    limit: Optional[int] = Field(default=None, ge=0, le=1_000_000_000)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("key", mode="before")
    @classmethod
    def _normalize_key(cls, value: Any) -> str:
        return str(value or "").strip().lower()

    @field_validator("metadata", mode="before")
    @classmethod
    def _normalize_metadata(cls, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return dict(value)


class BillingPlanEntitlementUpdateRequest(BaseModel):
    key: Optional[str] = Field(default=None, min_length=2, max_length=128, pattern=ENTITLEMENT_KEY_PATTERN)
    enabled: Optional[bool] = None
    value: Any = None
    limit: Optional[int] = Field(default=None, ge=0, le=1_000_000_000)
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("key", mode="before")
    @classmethod
    def _normalize_key(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value or "").strip().lower() or None

    @field_validator("metadata", mode="before")
    @classmethod
    def _normalize_metadata(cls, value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return dict(value)


class BillingEntitlementsMeResponse(BaseModel):
    user_id: str
    entitlements: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class BillingBindUserPlanRequest(BaseModel):
    plan_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    plan_code: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern=PLAN_CODE_PATTERN)
    duration_days: Optional[int] = Field(default=None, ge=1, le=36500)
    auto_renew: bool = False

    @field_validator("plan_id", "plan_code", mode="before")
    @classmethod
    def _normalize_plan_key(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value or "").strip() or None

    @model_validator(mode="after")
    def _validate_plan_selector(self) -> "BillingBindUserPlanRequest":
        if not self.plan_id and not self.plan_code:
            raise ValueError("plan_id or plan_code is required")
        return self


class BillingCheckoutRequest(BaseModel):
    plan_code: str = Field(..., min_length=1, max_length=64, pattern=PLAN_CODE_PATTERN)
    success_url: str = ""
    cancel_url: str = ""
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=IDEMPOTENCY_KEY_PATTERN)

    @field_validator("plan_code", mode="before")
    @classmethod
    def _strip_plan_code(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _strip_idempotency_key(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("success_url", "cancel_url", mode="before")
    @classmethod
    def _sanitize_urls(cls, value: Any, info: Any) -> str:
        field_name = str(getattr(info, "field_name", "url"))
        return _require_safe_input(field_name, str(value or ""))


class BillingCheckoutResponse(BaseModel):
    provider: str
    checkout_url: str
    checkout_payload: Dict[str, Any] = Field(default_factory=dict)
    order_id: str
    external_order_id: str


class ProductDeployActionResponse(BaseModel):
    product_id: str
    action_type: str
    label: str
    url: Optional[str] = None
    deploy_supported: bool = False
    detail: Optional[str] = None


class BillingSubscriptionSnapshot(BaseModel):
    subscription_id: Optional[str] = None
    status: str
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None
    expires_at: Optional[str] = None


class BillingPointsSnapshot(BaseModel):
    user_id: str
    balance: int


class BillingPointHistoryItem(BaseModel):
    flow_id: str
    flow_type: str
    points: int
    balance_after: Optional[int] = None
    note: Optional[str] = None
    order_id: Optional[str] = None
    subscription_id: Optional[str] = None
    occurred_at: Optional[str] = None


class AdminUserBillingStatusResponse(BaseModel):
    username: str
    role: str
    active: bool
    tenant_id: Optional[str] = None
    tenant_code: Optional[str] = None
    tenant_name: Optional[str] = None
    subscription: BillingSubscriptionSnapshot
    points_balance: int


class TenantWorkspacePlanBrief(BaseModel):
    code: str
    name: str
    currency: str
    price_cents: int
    monthly_points: int
    active: bool


class TenantWorkspaceSnapshot(BaseModel):
    user: AuthUserInfo
    tenant: Optional[TenantInfo] = None
    member_count: int = 1
    subscription: BillingSubscriptionSnapshot
    points: BillingPointsSnapshot
    available_plans: List[TenantWorkspacePlanBrief] = Field(default_factory=list)


class PaymentWebhookResponse(BaseModel):
    status: str
    reason: Optional[str] = None
    event_id: Optional[str] = None
    order_id: Optional[str] = None
    subscription_id: Optional[str] = None
    point_flow_id: Optional[str] = None


class BillingDevSimulatePaymentRequest(BaseModel):
    external_order_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=EXTERNAL_ORDER_ID_PATTERN,
        description="external order id returned by /billing/checkout",
    )
    event_id: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=IDEMPOTENCY_KEY_PATTERN, description="optional idempotency key for the simulated event")

    @field_validator("external_order_id", mode="before")
    @classmethod
    def _strip_external_order_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("event_id", mode="before")
    @classmethod
    def _strip_event_id(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value).strip()


class BillingExpireSubscriptionsResponse(BaseModel):
    expired: int


class BillingOrderResponse(BaseModel):
    order_id: str
    user_id: str
    plan_id: str
    plan_code: Optional[str] = None
    provider: str
    external_order_id: Optional[str] = None
    amount_cents: int
    currency: str
    status: str
    created_at: Optional[str] = None
    paid_at: Optional[str] = None


class BillingMyOrderStatusResponse(BaseModel):
    order_id: str
    external_order_id: str
    status: str
    plan_code: Optional[str] = None
    amount_cents: int
    currency: str
    created_at: Optional[str] = None
    paid_at: Optional[str] = None


class BillingAuditLogResponse(BaseModel):
    log_id: str
    occurred_at: str
    provider: str
    event_type: str
    external_event_id: Optional[str] = None
    external_order_id: Optional[str] = None
    signature_valid: bool
    outcome: str
    detail: Optional[str] = None


class BillingAuditLogDetailResponse(BillingAuditLogResponse):
    signature: Optional[str] = None
    raw_payload: str


class CaseResponse(BaseModel):
    case_id: str
    tenant_id: Optional[str] = None
    owner_username: Optional[str] = None
    status: str
    stage: str
    mode: Optional[str] = None
    run_mode: Optional[str] = None
    one_click_deploy: bool = False
    deploy_points_cost: int = 0
    commit_sha: Optional[str] = None
    resolved_ref: Optional[str] = None
    analyze_status: Optional[str] = None
    runtime: RuntimeInfo = Field(default_factory=RuntimeInfo)
    env_keys: List[str] = Field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    archived: bool = False
    archived_at: Optional[float] = None
    attempt: Optional[int] = None
    retry_of: Optional[str] = None
    repo_url: Optional[str] = None
    ref: Optional[str] = None
    branch: Optional[str] = None
    container_port: Optional[int] = None
    dockerfile_path: Optional[str] = None
    context_path: Optional[str] = None
    resolved_dockerfile_path: Optional[str] = None
    resolved_context_path: Optional[str] = None
    docker_build_network: Optional[str] = None
    docker_no_cache: Optional[bool] = None
    build_arg_keys: List[str] = Field(default_factory=list)
    git_submodules: Optional[bool] = None
    git_lfs: Optional[bool] = None
    container_id: Optional[str] = None
    host_port: Optional[int] = None
    access_url: Optional[str] = None
    compose_project_name: Optional[str] = None
    default_account: Optional[str] = None
    image_tag: Optional[str] = None
    last_log_at: Optional[float] = None
    report_ready: bool = False
    report_cached: Optional[bool] = None
    analyze_error_code: Optional[str] = None
    analyze_error_message: Optional[str] = None
    visual_status: Optional[str] = None
    visual_ready: bool = False
    visual_cached: Optional[bool] = None
    visual_error_code: Optional[str] = None
    visual_error_message: Optional[str] = None
    manual_status: Optional[str] = None
    manual_generated_at: Optional[float] = None
    manual_error_code: Optional[str] = None
    manual_error_message: Optional[str] = None
    manual_meta: Optional[ManualMeta] = None
    preflight_meta: Optional[PreflightMeta] = None
    repo_type: Optional[str] = None
    repo_evidence: List[str] = Field(default_factory=list)
    strategy_selected: Optional[str] = None
    strategy_reason: Optional[str] = None
    fallback_reason: Optional[str] = None
    generated_files: List[str] = Field(default_factory=list)


class CaseListResponse(BaseModel):
    items: List[CaseResponse]
    total: int
    page: int
    size: int


class CaseActionResponse(BaseModel):
    case_id: str
    action: str
    status: str
    message: str


class ManualResponse(BaseModel):
    case_id: str
    manual_markdown: str
    meta: ManualMeta


class ManualStatusResponse(BaseModel):
    case_id: str
    status: str
    generated_at: Optional[float] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class AnalyzeRequest(BaseModel):
    force: bool = False
    mode: str = "light"


class MarketingCard(BaseModel):
    title: str
    description: str


class BusinessSummary(BaseModel):
    slogan: str
    business_values: List[str]
    business_scenarios: List[str]
    readme_marketing_cards: List[MarketingCard]
    source: Optional[str] = None
    readme_cards_source: Optional[str] = None


class ReportResponse(BaseModel):
    markdown: str
    mermaids: List[str]
    assets: List[str]
    validation: Dict[str, Any]
    commit_sha: str
    created_at: float
    business_summary: Optional[BusinessSummary] = None


class VisualizeRequest(BaseModel):
    force: bool = False
    kinds: Optional[List[str]] = None


class VisualizeVideoRequest(BaseModel):
    force: bool = False


class UnderstandRequest(BaseModel):
    force: bool = False


class VisualFile(BaseModel):
    name: str
    url: str
    mime: Optional[str] = None


class VisualAssetResponse(BaseModel):
    kind: str
    status: str
    files: List[VisualFile]
    meta: Dict[str, Any] = Field(default_factory=dict)
    created_at: float
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class VisualsResponse(BaseModel):
    case_id: str
    commit_sha: str
    status: str
    assets: List[VisualAssetResponse]
    created_at: float
    cached: Optional[bool] = None
    template_version: Optional[str] = None


class UnderstandStatusResponse(BaseModel):
    case_id: str
    repo_url: Optional[str] = None
    state: str
    message: str
    visual_status: Optional[str] = None
    visual_ready: bool = False
    visual_error_code: Optional[str] = None
    visual_error_message: Optional[str] = None
    updated_at: Optional[float] = None


class UnderstandResultResponse(BaseModel):
    case_id: str
    repo_url: Optional[str] = None
    state: str
    status: str
    message: str
    assets: List[VisualAssetResponse]
    created_at: float
    cached: Optional[bool] = None


class TemplateExpected(BaseModel):
    status: Optional[str] = None
    error_code: Optional[str] = None
    note: Optional[str] = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip().upper()
        if normalized in EXPECTED_STATUS_VALUES:
            return normalized
        return None

    @field_validator("error_code", mode="before")
    @classmethod
    def _normalize_error_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip().upper()
        normalized = EXPECTED_ERROR_ALIASES.get(normalized, normalized)
        if normalized in ERROR_CODE_MAP:
            return normalized
        return None


class CaseTemplate(BaseModel):
    template_id: Optional[str] = None
    name: str
    group: Optional[str] = None
    description: Optional[str] = None
    dimensions: List[str] = Field(default_factory=list)
    expected: Optional[TemplateExpected] = None
    what_to_verify: Optional[str] = None
    repo_url: Optional[str] = None
    git_url: Optional[str] = None
    ref: Optional[str] = None
    default_mode: Optional[str] = None
    dockerfile_path: Optional[str] = None
    context_path: Optional[str] = None
    default_env_keys: List[str] = Field(default_factory=list)
    build_mode: Optional[str] = None
    port_mode: Optional[str] = None
    timeouts: Dict[str, int] = Field(default_factory=dict)


def normalize_stage(raw_stage: Optional[str], status: Optional[str]) -> str:
    if raw_stage:
        normalized = raw_stage.lower()
        if normalized in STAGE_VALUES:
            return normalized
    if status:
        return STATUS_STAGE_MAP.get(status.upper(), "system")
    return "system"


def normalize_run_mode(raw_run_mode: Optional[str], raw_mode: Optional[str], auto_mode: bool) -> str:
    candidate = (raw_run_mode or raw_mode or "").strip().lower()
    if candidate == "deploy":
        candidate = "container"
    if not candidate:
        candidate = "auto" if auto_mode else "auto"
    if candidate not in {"auto", "container", "showcase", "compose"}:
        candidate = "auto"
    return candidate


def _run_mode_requires_runtime(run_mode: str) -> bool:
    normalized = str(run_mode or "").strip().lower()
    return normalized in {"auto", "container", "compose"}


def build_case_response(case_id: str, data: Dict[str, Any]) -> CaseResponse:
    status = (data.get("status") or "PENDING").upper()
    stage = normalize_stage(data.get("stage"), status)
    runtime_data = data.get("runtime") or {}
    if not isinstance(runtime_data, dict):
        runtime_data = {}
    runtime = RuntimeInfo(**runtime_data)
    container_id = data.get("container_id") or runtime.container_id
    host_port = data.get("host_port") or runtime.host_port
    access_url = data.get("access_url") or runtime.access_url
    runtime = RuntimeInfo(
        container_id=container_id,
        host_port=host_port,
        access_url=access_url,
        url=access_url,
        started_at=runtime.started_at,
        exited_at=runtime.exited_at,
        exit_code=runtime.exit_code,
    )
    env_keys = sorted(data.get("env_keys") or [])
    error_code = data.get("error_code")
    error_message = data.get("error_message")
    if status == "FAILED":
        if not error_code:
            error_code = "UNEXPECTED_ERROR"
        if not error_message:
            error_message = "Unknown failure"
    ref_value = data.get("ref") or data.get("branch")
    branch_value = data.get("branch") or ref_value
    preflight_raw = data.get("preflight_meta")
    preflight_meta = None
    if isinstance(preflight_raw, dict):
        preflight_meta = PreflightMeta(**preflight_raw)
    manual_meta_raw = data.get("manual_meta")
    manual_meta = None
    if isinstance(manual_meta_raw, dict):
        manual_meta = ManualMeta(**manual_meta_raw)
    manual_status_raw = str(data.get("manual_status") or "").upper()
    if manual_status_raw not in {"PENDING", "RUNNING", "SUCCESS", "FAILED"}:
        manual_status_raw = "PENDING"
    analyze_status_raw = str(data.get("analyze_status") or "").upper()
    if analyze_status_raw not in {"PENDING", "RUNNING", "FINISHED", "FAILED"}:
        analyze_status_raw = "PENDING"
    visual_status_raw = str(data.get("visual_status") or "").upper()
    if visual_status_raw not in {"PENDING", "RUNNING", "SUCCESS", "FAILED", "PARTIAL", "NOT_STARTED"}:
        visual_status_raw = "PENDING"
    return CaseResponse(
        case_id=case_id,
        tenant_id=str(data.get("tenant_id") or "").strip() or None,
        owner_username=str(data.get("owner_username") or "").strip() or None,
        status=status,
        stage=stage,
        mode=data.get("mode"),
        run_mode=data.get("run_mode"),
        one_click_deploy=bool(data.get("one_click_deploy", False)),
        deploy_points_cost=max(0, int(data.get("deploy_points_cost") or 0)),
        commit_sha=data.get("commit_sha"),
        resolved_ref=data.get("resolved_ref"),
        analyze_status=analyze_status_raw,
        runtime=runtime,
        env_keys=env_keys,
        error_code=error_code,
        error_message=error_message,
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        archived=bool(data.get("archived", False)),
        archived_at=data.get("archived_at"),
        attempt=data.get("attempt"),
        retry_of=data.get("retry_of"),
        repo_url=data.get("repo_url"),
        ref=ref_value,
        branch=branch_value,
        container_port=data.get("container_port"),
        dockerfile_path=data.get("dockerfile_path"),
        compose_file=data.get("compose_file"),
        context_path=data.get("context_path"),
        resolved_dockerfile_path=data.get("resolved_dockerfile_path"),
        resolved_context_path=data.get("resolved_context_path"),
        docker_build_network=data.get("docker_build_network"),
        docker_no_cache=data.get("docker_no_cache"),
        build_arg_keys=sorted(data.get("build_arg_keys") or []),
        git_submodules=data.get("git_submodules"),
        git_lfs=data.get("git_lfs"),
        container_id=container_id,
        host_port=host_port,
        access_url=access_url,
        compose_project_name=data.get("compose_project_name"),
        default_account=data.get("default_account"),
        image_tag=data.get("image_tag"),
        last_log_at=data.get("last_log_at"),
        report_ready=bool(data.get("report_ready", False)),
        report_cached=data.get("report_cached"),
        analyze_error_code=data.get("analyze_error_code"),
        analyze_error_message=data.get("analyze_error_message"),
        visual_status=visual_status_raw,
        visual_ready=bool(data.get("visual_ready", False)),
        visual_cached=data.get("visual_cached"),
        visual_error_code=data.get("visual_error_code"),
        visual_error_message=data.get("visual_error_message"),
        manual_status=manual_status_raw,
        manual_generated_at=data.get("manual_generated_at"),
        manual_error_code=data.get("manual_error_code"),
        manual_error_message=data.get("manual_error_message"),
        manual_meta=manual_meta,
        preflight_meta=preflight_meta,
        repo_type=data.get("repo_type"),
        repo_evidence=sorted(data.get("repo_evidence") or []),
        strategy_selected=data.get("strategy_selected"),
        strategy_reason=data.get("strategy_reason"),
        fallback_reason=data.get("fallback_reason"),
        generated_files=sorted(data.get("generated_files") or []),
    )


def _guess_visual_mime(name: str) -> Optional[str]:
    lowered = name.lower()
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".svg"):
        return "image/svg+xml"
    if lowered.endswith(".jpg") or lowered.endswith(".jpeg"):
        return "image/jpeg"
    if lowered.endswith(".webp"):
        return "image/webp"
    if lowered.endswith(".mp4"):
        return "video/mp4"
    if lowered.endswith(".webm"):
        return "video/webm"
    if lowered.endswith(".wav"):
        return "audio/wav"
    if lowered.endswith(".mp3"):
        return "audio/mpeg"
    if lowered.endswith(".json"):
        return "application/json"
    return None


def build_visuals_response(case_id: str, commit_sha: str, payload: Dict[str, Any]) -> VisualsResponse:
    assets_payload = payload.get("assets") or []
    assets: List[VisualAssetResponse] = []
    for item in assets_payload:
        if not isinstance(item, dict):
            continue
        files: List[VisualFile] = []
        for name in item.get("files") or []:
            if not isinstance(name, str):
                continue
            files.append(
                VisualFile(
                    name=name,
                    url=f"/cases/{case_id}/visuals/{name}",
                    mime=_guess_visual_mime(name),
                )
            )
        assets.append(
            VisualAssetResponse(
                kind=str(item.get("kind") or ""),
                status=str(item.get("status") or "UNKNOWN"),
                files=files,
                meta=item.get("meta") or {},
                created_at=float(item.get("created_at") or payload.get("created_at") or 0),
                error_code=item.get("error_code"),
                error_message=item.get("error_message"),
            )
        )
    return VisualsResponse(
        case_id=case_id,
        commit_sha=str(payload.get("commit_sha") or commit_sha),
        status=str(payload.get("status") or "UNKNOWN"),
        assets=assets,
        created_at=float(payload.get("created_at") or 0),
        cached=payload.get("cached"),
        template_version=payload.get("template_version"),
    )


def _infer_understand_state(case_id: str, data: Dict[str, Any]) -> Tuple[str, str]:
    visual_status = str(data.get("visual_status") or "").upper()
    visual_ready = bool(data.get("visual_ready"))
    if visual_status == "FAILED":
        return "FAILED", "讲解失败，已返回可用的内容。"
    if visual_ready or visual_status in {"SUCCESS", "PARTIAL"}:
        return "DONE", "讲解已生成。"

    logs = get_logs_slice(case_id, offset=0, limit=200)
    visualize_logs = [entry for entry in logs if entry.get("stream") == "visualize"]
    latest = " ".join(str(entry.get("line") or "") for entry in visualize_logs[-5:])
    latest = latest.lower()

    if "ingest" in latest or "openclaw" in latest:
        return "FETCHING_REPOSITORY", "正在获取仓库内容。"
    if "repo_index" in latest or "repo_graph" in latest or "spotlights" in latest:
        return "UNDERSTANDING_CODE", "正在理解代码结构与核心逻辑。"
    if "storyboard" in latest or "render video" in latest:
        return "GENERATING_EXPLANATION", "正在生成讲解内容。"
    if visual_status in {"PENDING", "RUNNING"}:
        return "FETCHING_REPOSITORY", "正在准备理解仓库。"
    return "IDLE", "等待开始。"


def matches_query(case_id: str, data: Dict[str, Any], query: str) -> bool:
    q = query.lower().strip()
    if not q:
        return True
    haystacks = [
        case_id,
        str(data.get("repo_url") or ""),
        str(data.get("resolved_ref") or data.get("ref") or data.get("branch") or ""),
    ]
    return any(q in (value or "").lower() for value in haystacks)


def get_case_or_404(case_id: str) -> Dict[str, Any]:
    data = get_case(case_id) or {}
    if not data:
        raise HTTPException(status_code=404, detail="Case not found")
    return data


def _request_identity_for_cases(request: Request | None) -> AuthIdentity:
    if request is None:
        # Backward-compatible path for direct function calls in unit tests.
        return AuthIdentity(username="system", role="root")
    identity = getattr(request.state, "auth_identity", None)
    if isinstance(identity, AuthIdentity):
        return identity
    if not AUTH_ENABLED:
        return AuthIdentity(username="anonymous", role="admin")
    raise HTTPException(status_code=401, detail="unauthorized")


def _resolve_identity_tenant_for_cases(identity: AuthIdentity) -> str | None:
    claimed = str(identity.tenant_id or "").strip() or None
    with session_scope() as session:
        if session is None:
            return claimed
        repo = BillingRepository(session)
        return _identity_tenant_id(repo, identity) or claimed


def _case_tenant_id(data: Dict[str, Any]) -> str | None:
    return str(data.get("tenant_id") or "").strip() or None


def _case_owner_username(data: Dict[str, Any]) -> str | None:
    return str(data.get("owner_username") or "").strip() or None


def _is_case_visible_to_identity(
    identity: AuthIdentity,
    data: Dict[str, Any],
    *,
    actor_tenant_id: str | None = None,
) -> bool:
    if not AUTH_ENABLED:
        return True
    if _is_root_role(identity):
        return True

    resolved_actor_tenant_id = actor_tenant_id
    if resolved_actor_tenant_id is None:
        resolved_actor_tenant_id = _resolve_identity_tenant_for_cases(identity)

    case_tenant_id = _case_tenant_id(data)
    if case_tenant_id:
        return bool(resolved_actor_tenant_id and str(resolved_actor_tenant_id) == str(case_tenant_id))

    # Legacy global cases without tenant_id fallback to owner binding.
    owner_username = _case_owner_username(data)
    if owner_username:
        return owner_username == identity.username
    return False


def _get_case_for_identity(
    case_id: str,
    identity: AuthIdentity,
    *,
    actor_tenant_id: str | None = None,
) -> Dict[str, Any]:
    data = get_case_or_404(case_id)
    if _is_case_visible_to_identity(identity, data, actor_tenant_id=actor_tenant_id):
        return data
    raise HTTPException(status_code=404, detail="Case not found")


def get_managed_container(case_id: str, container_id: str) -> docker.models.containers.Container:
    client = docker.from_env()
    container = client.containers.get(container_id)
    labels = container.labels or {}
    if labels.get(CASE_LABEL_KEY) != case_id or labels.get(CASE_LABEL_MANAGED) != "true":
        raise HTTPException(status_code=400, detail="Container is not managed by AntiHub")
    return container


def append_system_log(case_id: str, line: str, level: str = "INFO") -> None:
    append_log(case_id, {"ts": time.time(), "stream": "system", "level": level, "line": line})


def run_compose_down(
    case_id: str,
    repo_dir: Optional[str],
    compose_file: Optional[str],
    project_name: Optional[str],
) -> None:
    if not repo_dir or not compose_file or not project_name:
        raise HTTPException(status_code=400, detail="Missing compose info for cleanup")
    repo_path = Path(repo_dir)
    compose_path = repo_path / compose_file
    if not compose_path.exists():
        raise HTTPException(status_code=400, detail="compose file not found for cleanup")
    cmd = ["docker", "compose", "-f", str(compose_path), "down", "--remove-orphans", "--volumes"]
    env = dict(os.environ)
    env["COMPOSE_PROJECT_NAME"] = project_name
    append_system_log(case_id, f"[compose] exec: {' '.join(cmd)}")
    try:
        with subprocess.Popen(  # nosec B603
            cmd,
            cwd=str(repo_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        ) as proc:
            if proc.stdout:
                for raw in proc.stdout:
                    line = raw.rstrip("\n")
                    if line:
                        append_system_log(case_id, line)
            proc.wait()
    except FileNotFoundError as exc:
        append_system_log(case_id, "docker compose not available", level="ERROR")
        raise HTTPException(status_code=500, detail=f"compose not available: {exc}") from exc


def build_jsonl(entries: List[Dict[str, Any]]) -> str:
    return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + ("\n" if entries else "")


def resolve_host_port(container: docker.models.containers.Container, container_port: Optional[int]) -> Optional[int]:
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    if container_port:
        binding = ports.get(f"{container_port}/tcp")
        if binding:
            return int(binding[0]["HostPort"])
    for binding in ports.values():
        if binding:
            return int(binding[0]["HostPort"])
    return None


def _to_plan_response(plan: Any) -> BillingPlanResponse:
    return BillingPlanResponse(
        plan_id=str(plan.id),
        code=str(plan.code),
        name=str(plan.name),
        description=plan.description,
        currency=str(plan.currency),
        price_cents=int(plan.price_cents),
        monthly_points=int(plan.monthly_points),
        active=bool(plan.active),
    )


def _to_saas_plan_response(plan: Any) -> SaaSPlanResponse:
    metadata_value = getattr(plan, "metadata_json", None)
    if not isinstance(metadata_value, dict):
        metadata_value = {}
    return SaaSPlanResponse(
        plan_id=str(plan.id),
        code=str(plan.code),
        name=str(plan.name),
        description=plan.description,
        currency=str(plan.currency),
        price_cents=int(plan.price_cents),
        monthly_points=int(plan.monthly_points),
        active=bool(plan.active),
        billing_cycle=(str(getattr(plan, "billing_cycle", "") or "").strip().lower() or "monthly"),
        trial_days=(
            int(getattr(plan, "trial_days", 0))
            if getattr(plan, "trial_days", None) is not None
            else 0
        ),
        metadata=dict(metadata_value),
    )


def _to_workspace_plan_brief(plan: Any) -> TenantWorkspacePlanBrief:
    return TenantWorkspacePlanBrief(
        code=str(plan.code),
        name=str(plan.name),
        currency=str(plan.currency),
        price_cents=int(plan.price_cents),
        monthly_points=int(plan.monthly_points),
        active=bool(plan.active),
    )


def _to_plan_entitlement_response(item: Any) -> BillingPlanEntitlementResponse:
    metadata_value = getattr(item, "metadata_json", None)
    if not isinstance(metadata_value, dict):
        metadata_value = {}
    limit_raw = getattr(item, "limit_value", None)
    return BillingPlanEntitlementResponse(
        entitlement_id=str(getattr(item, "id", "")),
        plan_id=str(getattr(item, "plan_id", "")),
        key=str(getattr(item, "key", "")),
        enabled=bool(getattr(item, "enabled", False)),
        value=getattr(item, "value_json", None),
        limit=(int(limit_raw) if limit_raw is not None else None),
        metadata=dict(metadata_value),
    )


def _duration_days_from_plan(plan: Any) -> int:
    cycle = str(getattr(plan, "billing_cycle", "") or "").strip().lower()
    if cycle == "yearly":
        return 365
    if cycle == "monthly":
        return 30
    return 30


def _to_tenant_response(tenant: Any) -> TenantInfo:
    return TenantInfo(
        tenant_id=str(tenant.id),
        code=str(tenant.code),
        name=str(tenant.name),
        active=bool(getattr(tenant, "active", True)),
    )


def _to_order_response(order: Any) -> BillingOrderResponse:
    plan_code = None
    try:
        plan = getattr(order, "plan", None)
        plan_code = getattr(plan, "code", None) if plan else None
    except Exception:
        plan_code = None
    return BillingOrderResponse(
        order_id=str(order.id),
        user_id=str(order.user_id),
        plan_id=str(order.plan_id),
        plan_code=str(plan_code) if plan_code is not None else None,
        provider=str(getattr(order, "provider", "") or ""),
        external_order_id=str(order.external_order_id) if getattr(order, "external_order_id", None) else None,
        amount_cents=int(getattr(order, "amount_cents", 0) or 0),
        currency=str(getattr(order, "currency", "") or ""),
        status=str(order.status.value if hasattr(order.status, "value") else order.status),
        created_at=order.created_at.isoformat() if getattr(order, "created_at", None) else None,
        paid_at=order.paid_at.isoformat() if getattr(order, "paid_at", None) else None,
    )


def _to_audit_log_response(log: Any) -> BillingAuditLogResponse:
    return BillingAuditLogResponse(
        log_id=str(log.id),
        occurred_at=log.occurred_at.isoformat() if getattr(log, "occurred_at", None) else "",
        provider=str(getattr(log, "provider", "") or ""),
        event_type=str(getattr(log, "event_type", "") or ""),
        external_event_id=str(log.external_event_id) if getattr(log, "external_event_id", None) else None,
        external_order_id=str(log.external_order_id) if getattr(log, "external_order_id", None) else None,
        signature_valid=bool(getattr(log, "signature_valid", False)),
        outcome=str(getattr(log, "outcome", "") or ""),
        detail=str(log.detail) if getattr(log, "detail", None) else None,
    )


def _to_audit_log_detail_response(log: Any) -> BillingAuditLogDetailResponse:
    base = _to_audit_log_response(log)
    return BillingAuditLogDetailResponse(
        **base.model_dump(),
        signature=str(log.signature) if getattr(log, "signature", None) else None,
        raw_payload=str(getattr(log, "raw_payload", "") or ""),
    )


def _safe_pool_metric(pool: Any, metric: str) -> Optional[int]:
    candidate = getattr(pool, metric, None)
    if not callable(candidate):
        return None
    try:
        value = candidate()
    except Exception:
        return None
    if value is None:
        return None
    return int(value)


def _build_runtime_health_report() -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "status": "ok",
        "api": "ok",
        "redis": "ok",
        "db": "ok",
        "docker": "ok",
        "openclaw": "missing",
        "disk": "ok",
        "version": app.version,
        "git_sha": GIT_SHA,
        "root_path": ROOT_PATH,
        "api_host": API_HOST,
        "api_port": API_PORT,
        "details": {},
    }

    if REDIS_DISABLED:
        report["redis"] = "disabled"
    else:
        started = time.perf_counter()
        try:
            client = get_redis_client()
            client.ping()
            report["details"]["redis_latency_ms"] = int((time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            report["redis"] = "error"
            report["details"]["redis"] = str(exc)
            report["status"] = "degraded"

    db_started = time.perf_counter()
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        report["details"]["db_latency_ms"] = int((time.perf_counter() - db_started) * 1000)
    except Exception as exc:  # noqa: BLE001
        report["db"] = "error"
        report["details"]["db"] = str(exc)
        report["status"] = "degraded"

    pool = getattr(BILLING_ENGINE, "pool", None)
    if pool is not None:
        pool_size = _safe_pool_metric(pool, "size")
        checked_out = _safe_pool_metric(pool, "checkedout")
        overflow = _safe_pool_metric(pool, "overflow")
        pool_metrics: Dict[str, Any] = {"pool_class": type(pool).__name__}
        if pool_size is not None:
            pool_metrics["size"] = pool_size
        if checked_out is not None:
            pool_metrics["checked_out"] = checked_out
        if overflow is not None:
            pool_metrics["overflow"] = overflow
        if pool_size and checked_out is not None:
            saturation = round(checked_out / max(1, pool_size), 4)
            pool_metrics["saturation"] = saturation
            if saturation >= 0.9 and report["status"] == "ok":
                report["status"] = "degraded"
                report["details"]["db_pool_warning"] = "connection pool saturation >= 90%"
        report["details"]["db_pool"] = pool_metrics

    try:
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        report["docker"] = "error"
        report["details"]["docker"] = str(exc)
        report["status"] = "degraded"

    if OPENCLAW_BASE_URL:
        try:
            parsed = urlparse(OPENCLAW_BASE_URL)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not host:
                raise ValueError("missing host")
            with socket.create_connection((host, port), timeout=1.5):
                pass
            report["openclaw"] = "ok"
        except Exception as exc:  # noqa: BLE001
            report["openclaw"] = "error"
            report["details"]["openclaw"] = str(exc)
            report["status"] = "degraded"
    else:
        report["details"]["openclaw"] = "OPENCLAW_BASE_URL is missing"
        report["status"] = "degraded"

    try:
        total, used, free = shutil.disk_usage(Path(__file__).resolve().parent)
        free_ratio = free / max(1, total)
        report["details"]["disk"] = {
            "total_bytes": int(total),
            "used_bytes": int(used),
            "free_bytes": int(free),
            "free_ratio": round(free_ratio, 4),
        }
        if free_ratio < 0.1:
            report["disk"] = "degraded"
            report["status"] = "degraded"
    except Exception as exc:  # noqa: BLE001
        report["disk"] = "error"
        report["details"]["disk"] = str(exc)
        report["status"] = "degraded"

    return report


@app.get("/health")
async def health() -> dict:
    return _build_runtime_health_report()


@app.get("/healthz")
async def healthz() -> dict:
    return _build_runtime_health_report()


@app.get("/health/billing")
async def health_billing() -> dict:
    report: Dict[str, Any] = {
        "status": "ok",
        "db": "ok",
        "config": "ok",
        "details": {},
    }
    internal_ready = bool(PAYMENT_WEBHOOK_SECRET)
    if not internal_ready:
        report["details"]["PAYMENT_WEBHOOK_SECRET"] = False

    wechat_checkout_ready = True
    if str(PAYMENT_PROVIDER or "").strip().lower() == "wechatpay":
        missing: list[str] = []
        if not WECHATPAY_API_BASE_URL:
            missing.append("WECHATPAY_API_BASE_URL")
        if not WECHATPAY_NOTIFY_URL:
            missing.append("WECHATPAY_NOTIFY_URL")
        if not WECHATPAY_MCHID:
            missing.append("WECHATPAY_MCHID")
        if not WECHATPAY_APPID:
            missing.append("WECHATPAY_APPID")
        if not WECHATPAY_CERT_SERIAL:
            missing.append("WECHATPAY_CERT_SERIAL")
        if not (WECHATPAY_PRIVATE_KEY_PEM or WECHATPAY_PRIVATE_KEY_PATH):
            missing.append("WECHATPAY_PRIVATE_KEY_PEM/WECHATPAY_PRIVATE_KEY_PATH")
        if missing:
            wechat_checkout_ready = False
            report["details"]["wechatpay_checkout"] = {"missing": missing}

    wechat_webhook_ready = True
    if str(PAYMENT_PROVIDER or "").strip().lower() == "wechatpay":
        missing: list[str] = []
        if not WECHATPAY_APIV3_KEY:
            missing.append("WECHATPAY_APIV3_KEY")
        try:
            platform_certs = parse_platform_certs(
                certs_json=WECHATPAY_PLATFORM_CERTS_JSON,
                cert_serial=WECHATPAY_PLATFORM_CERT_SERIAL,
                cert_pem=WECHATPAY_PLATFORM_CERT_PEM,
                cert_path=WECHATPAY_PLATFORM_CERT_PATH,
            )
        except Exception as exc:  # noqa: BLE001
            wechat_webhook_ready = False
            report["details"]["wechatpay_platform_certs"] = f"config_error: {exc}"
            platform_certs = {}
        if not platform_certs:
            missing.append("WECHATPAY_PLATFORM_CERTS_JSON or (WECHATPAY_PLATFORM_CERT_SERIAL + PEM/PATH)")
        if missing:
            wechat_webhook_ready = False
            report["details"]["wechatpay_webhook"] = {"missing": missing}

    if str(PAYMENT_PROVIDER or "").strip().lower() == "wechatpay":
        if not wechat_checkout_ready or not wechat_webhook_ready:
            report["config"] = "error"
            report["status"] = "error"
    else:
        if not internal_ready:
            report["config"] = "error"
            report["status"] = "error"
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        report["db"] = "error"
        report["status"] = "error"
        report["details"]["db"] = str(exc)
    return report


@app.get("/metrics/runtime")
async def runtime_metrics(_: AuthIdentity = Depends(require_admin)) -> dict:
    return get_runtime_metrics_snapshot()


@app.post("/auth/login", response_model=AuthLoginResponse)
async def auth_login(payload: AuthLoginRequest) -> AuthLoginResponse:
    if not AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="authentication is disabled")
    if not AUTH_TOKEN_SECRET:
        raise HTTPException(status_code=503, detail="AUTH_TOKEN_SECRET is missing")

    identity = _authenticate_user_from_db(payload.username, payload.password)
    used_legacy_fallback = False
    if not identity and AUTH_USERS_JSON:
        identity = authenticate_user(payload.username, payload.password, AUTH_USERS_JSON)
        used_legacy_fallback = identity is not None
    if not identity:
        raise HTTPException(status_code=401, detail="invalid username or password")
    if used_legacy_fallback:
        _migrate_legacy_user_to_db(identity, payload.password)
    user_info = AuthUserInfo(username=identity.username, role=identity.role)
    try:
        with session_scope() as session:
            repo = BillingRepository(session)
            user_info = _to_auth_user_info(identity, repo)
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.login.userinfo_fallback", username=identity.username, error=str(exc))
    if user_info.tenant_id and not identity.tenant_id:
        identity = AuthIdentity(username=identity.username, role=identity.role, tenant_id=user_info.tenant_id)
    token = issue_access_token(identity, AUTH_TOKEN_SECRET, AUTH_TOKEN_TTL_SECONDS)
    return AuthLoginResponse(
        access_token=token,
        token_type="bearer",  # nosec B106
        expires_in=AUTH_TOKEN_TTL_SECONDS,
        user=user_info,
    )


@app.post("/auth/register", response_model=AuthLoginResponse)
async def auth_register(payload: AuthRegisterRequest) -> AuthLoginResponse:
    if not AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="authentication is disabled")
    if not AUTH_TOKEN_SECRET:
        raise HTTPException(status_code=503, detail="AUTH_TOKEN_SECRET is missing")

    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    with session_scope() as session:
        repo = BillingRepository(session)
        if repo.get_auth_user(username) is not None:
            raise HTTPException(status_code=409, detail="username already exists")

        tenant_name = (payload.tenant_name or "").strip()
        requested_tenant_code = _slugify_tenant_code(payload.tenant_code or "")
        if requested_tenant_code and len(requested_tenant_code) < 2:
            raise HTTPException(status_code=422, detail="tenant_code is too short")
        if not tenant_name:
            tenant_name = f"{username} workspace"

        if requested_tenant_code:
            tenant = repo.get_or_create_tenant(code=requested_tenant_code, name=tenant_name, active=True)
        else:
            generated_code = _derive_tenant_code(repo, tenant_name, username)
            tenant = repo.create_tenant(code=generated_code, name=tenant_name, active=True)

        user = repo.upsert_auth_user(
            username=username,
            password_hash=hash_password_bcrypt(payload.password),
            role="user",
            active=True,
            tenant_id=str(tenant.id),
        )
        identity = AuthIdentity(
            username=str(user.username),
            role=_normalize_role_value(str(user.role)),
            tenant_id=str(getattr(user, "tenant_id", "") or "").strip() or None,
        )
        user_info = _to_auth_user_info(identity, repo)

    token = issue_access_token(identity, AUTH_TOKEN_SECRET, AUTH_TOKEN_TTL_SECONDS)
    return AuthLoginResponse(
        access_token=token,
        token_type="bearer",  # nosec B106
        expires_in=AUTH_TOKEN_TTL_SECONDS,
        user=user_info,
    )


@app.get("/auth/me", response_model=AuthUserInfo)
async def auth_me(identity: AuthIdentity = Depends(get_current_identity)) -> AuthUserInfo:
    try:
        with session_scope() as session:
            repo = BillingRepository(session)
            return _to_auth_user_info(identity, repo)
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.me.userinfo_fallback", username=identity.username, error=str(exc))
        return AuthUserInfo(username=identity.username, role=identity.role)


@app.get("/auth/permissions/me", response_model=AuthPermissionSnapshot)
async def auth_permissions(identity: AuthIdentity = Depends(get_current_identity)) -> AuthPermissionSnapshot:
    tenant_id = str(identity.tenant_id or "").strip() or None
    try:
        with session_scope() as session:
            repo = BillingRepository(session)
            user = repo.get_auth_user(identity.username)
            if user is not None:
                tenant_id = str(getattr(user, "tenant_id", "") or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        log_event(APP_LOGGER, logging.WARNING, "auth.permissions.tenant_resolve_failed", error=str(exc))
    return AuthPermissionSnapshot(
        username=identity.username,
        role=_normalize_role_value(identity.role),
        tenant_id=tenant_id,
        scopes=_scopes_for_identity(identity),
    )


@app.post("/admin/tenants", response_model=TenantInfo)
@app.post("/org/tenants", response_model=TenantInfo)
async def create_tenant(
    payload: TenantCreateRequest,
    identity: AuthIdentity = Depends(require_admin),
) -> TenantInfo:
    if not _is_root_role(identity):
        raise HTTPException(status_code=403, detail="root role required")
    with session_scope() as session:
        repo = BillingRepository(session)
        if payload.code:
            tenant_code = payload.code
        else:
            tenant_code = _derive_tenant_code(repo, payload.name, payload.name)
        try:
            tenant = repo.create_tenant(code=tenant_code, name=payload.name, active=payload.active)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _to_tenant_response(tenant)


@app.put("/admin/tenants/{tenant_id}", response_model=TenantInfo)
@app.put("/org/tenants/{tenant_id}", response_model=TenantInfo)
async def update_tenant(
    tenant_id: str,
    payload: TenantUpdateRequest,
    identity: AuthIdentity = Depends(require_admin),
) -> TenantInfo:
    if not _is_root_role(identity):
        raise HTTPException(status_code=403, detail="root role required")
    if payload.code is None and payload.name is None and payload.active is None:
        raise HTTPException(status_code=400, detail="no fields to update")
    with session_scope() as session:
        repo = BillingRepository(session)
        try:
            tenant = repo.update_tenant(
                tenant_id,
                code=payload.code,
                name=payload.name,
                active=payload.active,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _to_tenant_response(tenant)


@app.get("/admin/tenants", response_model=List[TenantInfo])
@app.get("/org/tenants", response_model=List[TenantInfo])
async def list_tenants(
    include_inactive: bool = Query(True),
    identity: AuthIdentity = Depends(require_admin),
) -> List[TenantInfo]:
    with session_scope() as session:
        repo = BillingRepository(session)
        if not _is_root_role(identity):
            actor_tenant_id = _identity_tenant_id(repo, identity)
            if not actor_tenant_id:
                return []
            tenant = repo.get_tenant_by_id(actor_tenant_id)
            if tenant is None:
                return []
            if not include_inactive and not bool(getattr(tenant, "active", True)):
                return []
            return [_to_tenant_response(tenant)]
        tenants = repo.list_tenants(include_inactive=bool(include_inactive))
        return [_to_tenant_response(item) for item in tenants]


@app.get("/admin/tenants/{tenant_id}/users", response_model=List[AuthUserInfo])
@app.get("/org/tenants/{tenant_id}/users", response_model=List[AuthUserInfo])
async def list_tenant_users(
    tenant_id: str,
    include_inactive: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: AuthIdentity = Depends(require_admin),
) -> List[AuthUserInfo]:
    with session_scope() as session:
        repo = BillingRepository(session)
        _enforce_tenant_scope(repo, identity, tenant_id)
        tenant = repo.get_tenant_by_id(tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        users = repo.list_auth_users(
            include_inactive=bool(include_inactive),
            tenant_id=str(tenant.id),
            limit=limit,
            offset=offset,
        )
        items: list[AuthUserInfo] = []
        for user in users:
            item_identity = AuthIdentity(
                username=str(user.username),
                role=_normalize_role_value(str(user.role)),
                tenant_id=str(getattr(user, "tenant_id", "") or "").strip() or None,
            )
            items.append(_to_auth_user_info(item_identity, repo))
        return items


@app.get("/admin/users", response_model=List[AuthUserInfo])
@app.get("/org/users", response_model=List[AuthUserInfo])
async def list_org_users(
    tenant_id: str = Query("", description="optional tenant filter"),
    include_inactive: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: AuthIdentity = Depends(require_admin),
) -> List[AuthUserInfo]:
    with session_scope() as session:
        repo = BillingRepository(session)
        normalized_tenant_id = tenant_id.strip() or None
        if _is_root_role(identity):
            users = repo.list_auth_users(
                include_inactive=bool(include_inactive),
                tenant_id=normalized_tenant_id,
                limit=limit,
                offset=offset,
            )
        else:
            actor_tenant_id = _identity_tenant_id(repo, identity)
            if not actor_tenant_id:
                return []
            if normalized_tenant_id and normalized_tenant_id != actor_tenant_id:
                raise HTTPException(status_code=403, detail="cross-tenant access denied")
            users = repo.list_auth_users(
                include_inactive=bool(include_inactive),
                tenant_id=actor_tenant_id,
                limit=limit,
                offset=offset,
            )
        return [
            _to_auth_user_info(
                AuthIdentity(
                    username=str(user.username),
                    role=_normalize_role_value(str(user.role)),
                    tenant_id=str(getattr(user, "tenant_id", "") or "").strip() or None,
                ),
                repo,
            )
            for user in users
        ]


@app.post("/admin/users", response_model=AuthUserInfo)
@app.post("/org/users", response_model=AuthUserInfo)
async def create_org_user(
    payload: OrgUserCreateRequest,
    identity: AuthIdentity = Depends(require_admin),
) -> AuthUserInfo:
    with session_scope() as session:
        repo = BillingRepository(session)
        target_tenant_id = str(payload.tenant_id or "").strip() or None
        target_role = _normalize_role_value(payload.role)
        if _is_root_role(identity):
            pass
        else:
            actor_tenant_id = _identity_tenant_id(repo, identity)
            if not actor_tenant_id:
                raise HTTPException(status_code=403, detail="tenant scoped access denied")
            if target_tenant_id and target_tenant_id != actor_tenant_id:
                raise HTTPException(status_code=403, detail="cross-tenant access denied")
            target_tenant_id = actor_tenant_id
            if target_role != "user":
                raise HTTPException(status_code=403, detail="admin can only create user role")

        if target_role in {"admin", "user"} and not target_tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required for non-root users")
        if target_role == "root":
            target_tenant_id = None

        try:
            user = repo.create_auth_user(
                username=payload.username.strip(),
                password_hash=hash_password_bcrypt(payload.password),
                role=target_role,
                active=payload.active,
                tenant_id=target_tenant_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        identity_out = AuthIdentity(
            username=str(user.username),
            role=_normalize_role_value(str(user.role)),
            tenant_id=str(getattr(user, "tenant_id", "") or "").strip() or None,
        )
        return _to_auth_user_info(identity_out, repo)


@app.patch("/admin/users/{username}", response_model=AuthUserInfo)
@app.patch("/org/users/{username}", response_model=AuthUserInfo)
async def update_org_user(
    username: str,
    payload: OrgUserUpdateRequest,
    identity: AuthIdentity = Depends(require_admin),
) -> AuthUserInfo:
    if payload.password is None and payload.role is None and payload.tenant_id is None and payload.active is None:
        raise HTTPException(status_code=400, detail="no fields to update")

    with session_scope() as session:
        repo = BillingRepository(session)
        target = repo.get_auth_user(username)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")

        _enforce_user_management_scope(
            repo,
            identity,
            target_user=target,
            intended_role=payload.role,
            intended_tenant_id=payload.tenant_id,
        )

        next_role = payload.role
        next_tenant_id = payload.tenant_id
        if _is_root_role(identity):
            if next_role is not None and _normalize_role_value(next_role) in {"admin", "user"}:
                if str(next_tenant_id or getattr(target, "tenant_id", "") or "").strip() == "":
                    raise HTTPException(status_code=400, detail="tenant_id is required for non-root users")
            if next_role is not None and _normalize_role_value(next_role) == "root":
                next_tenant_id = ""
        else:
            next_role = payload.role if payload.role is not None else str(getattr(target, "role", "user"))
            next_tenant_id = payload.tenant_id if payload.tenant_id is not None else str(getattr(target, "tenant_id", "") or "")

        try:
            updated = repo.update_auth_user(
                username,
                role=next_role,
                active=payload.active,
                password_hash=hash_password_bcrypt(payload.password) if payload.password is not None else None,
                tenant_id=next_tenant_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        identity_out = AuthIdentity(
            username=str(updated.username),
            role=_normalize_role_value(str(updated.role)),
            tenant_id=str(getattr(updated, "tenant_id", "") or "").strip() or None,
        )
        return _to_auth_user_info(identity_out, repo)


@app.delete("/admin/users/{username}", response_model=AuthUserInfo)
@app.delete("/org/users/{username}", response_model=AuthUserInfo)
async def deactivate_org_user(
    username: str,
    identity: AuthIdentity = Depends(require_admin),
) -> AuthUserInfo:
    with session_scope() as session:
        repo = BillingRepository(session)
        target = repo.get_auth_user(username)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")
        if _normalize_role_value(str(getattr(target, "role", ""))) == "root" and not _is_root_role(identity):
            raise HTTPException(status_code=403, detail="cannot deactivate root user")
        _enforce_user_management_scope(repo, identity, target_user=target)
        try:
            updated = repo.deactivate_auth_user(username)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        identity_out = AuthIdentity(
            username=str(updated.username),
            role=_normalize_role_value(str(updated.role)),
            tenant_id=str(getattr(updated, "tenant_id", "") or "").strip() or None,
        )
        return _to_auth_user_info(identity_out, repo)


@app.get("/tenant/workspace", response_model=TenantWorkspaceSnapshot)
async def tenant_workspace(identity: AuthIdentity = Depends(get_current_identity)) -> TenantWorkspaceSnapshot:
    with session_scope() as session:
        repo = BillingRepository(session)
        user_info = _to_auth_user_info(identity, repo)
        tenant_obj = None
        member_count = 1
        if user_info.tenant_id:
            tenant_obj = repo.get_tenant_by_id(str(user_info.tenant_id))
            member_count = repo.count_auth_users(include_inactive=False, tenant_id=str(user_info.tenant_id))
            member_count = max(1, int(member_count))

        repo.expire_due_subscriptions()
        subscription = repo.get_active_subscription(identity.username)
        subscription_snapshot = BillingSubscriptionSnapshot(status="none")
        if subscription:
            plan = subscription.plan
            subscription_snapshot = BillingSubscriptionSnapshot(
                subscription_id=subscription.id,
                status=str(subscription.status.value if hasattr(subscription.status, "value") else subscription.status),
                plan_code=getattr(plan, "code", None),
                plan_name=getattr(plan, "name", None),
                expires_at=subscription.expires_at.isoformat() if subscription.expires_at else None,
            )
        points_snapshot = BillingPointsSnapshot(
            user_id=identity.username,
            balance=repo.get_user_point_balance(identity.username),
        )
        plans = repo.list_plans(include_inactive=_is_admin_role(identity))
        available_plans = [_to_workspace_plan_brief(plan) for plan in plans]
        return TenantWorkspaceSnapshot(
            user=user_info,
            tenant=_to_tenant_response(tenant_obj) if tenant_obj else None,
            member_count=member_count,
            subscription=subscription_snapshot,
            points=points_snapshot,
            available_plans=available_plans,
        )


@app.get("/billing/plans", response_model=List[BillingPlanResponse])
async def billing_plans(identity: AuthIdentity = Depends(get_current_identity)) -> List[BillingPlanResponse]:
    with session_scope() as session:
        repo = BillingRepository(session)
        plans = repo.list_plans(include_inactive=_is_admin_role(identity))
        return [_to_plan_response(plan) for plan in plans]


@app.get("/billing/entitlements/me", response_model=BillingEntitlementsMeResponse)
async def billing_entitlements_me(identity: AuthIdentity = Depends(get_current_identity)) -> BillingEntitlementsMeResponse:
    _ensure_feature_enabled(FEATURE_SAAS_ENTITLEMENTS)
    entitlements = get_user_entitlements(identity.username)
    return BillingEntitlementsMeResponse(user_id=identity.username, entitlements=entitlements)


@app.get("/billing/entitlements/check/deep-search", response_model=Dict[str, Any])
async def billing_entitlement_check_deep_search(_: Dict[str, Any] = Depends(require_entitlement("feature.deep_search"))) -> Dict[str, Any]:
    _ensure_feature_enabled(FEATURE_SAAS_ENTITLEMENTS)
    return {"allowed": True}


@app.post("/billing/checkout", response_model=BillingCheckoutResponse)
async def billing_checkout(
    payload: BillingCheckoutRequest,
    identity: AuthIdentity = Depends(get_current_identity),
) -> BillingCheckoutResponse:
    plan_code = payload.plan_code.strip()
    if not plan_code:
        raise HTTPException(status_code=400, detail="plan_code is required")

    provider = get_payment_provider()
    now = datetime.now(timezone.utc)

    # Keep DB transaction short: create (or fetch) order, then call provider outside the session.
    with session_scope() as session:
        repo = BillingRepository(session)
        plan = repo.get_plan_by_code(plan_code)
        if not plan or (not plan.active and not _is_admin_role(identity)):
            raise HTTPException(status_code=404, detail=f"plan not found: {plan_code}")

        external_order_id_seed = f"ord_ext_{uuid.uuid4().hex[:20]}"
        user_key = (payload.idempotency_key or "").strip()
        idempotency_key = (
            f"checkout:{identity.username}:{user_key}" if user_key else f"checkout:{identity.username}:{external_order_id_seed}"
        )

        order = repo.create_order(
            user_id=identity.username,
            plan_id=plan.id,
            amount_cents=int(plan.price_cents),
            currency=str(plan.currency or "usd"),
            provider=provider.name,
            external_order_id=external_order_id_seed,
            idempotency_key=idempotency_key,
        )

        if str(order.user_id) != str(identity.username):
            raise HTTPException(status_code=403, detail="not allowed")
        if str(order.plan_id) != str(plan.id):
            raise HTTPException(status_code=409, detail="idempotency_key conflicts with a different plan")
        if str(order.status.value if hasattr(order.status, "value") else order.status) != "pending":
            raise HTTPException(status_code=409, detail=f"order is not pending: {order.id}")

        order_id = str(order.id)
        external_order_id = str(order.external_order_id or external_order_id_seed)
        provider_name = str(getattr(order, "provider", "") or provider.name).strip().lower() or provider.name
        amount_cents = int(getattr(order, "amount_cents", 0) or 0)
        currency = str(getattr(order, "currency", "usd") or "usd")

        # Checkout idempotency: if we've already created a provider checkout_url for this order,
        # return it instead of calling the provider again (important for WeChat out_trade_no reuse).
        cached_checkout_url: Optional[str] = None
        cached_payload: Dict[str, Any] = {}
        if order.provider_payload:
            try:
                parsed = json.loads(str(order.provider_payload or ""))
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                checkout = parsed.get("checkout")
                if isinstance(checkout, dict):
                    cached_checkout_url = str(checkout.get("checkout_url") or "").strip() or None
                    raw = checkout.get("raw")
                    if isinstance(raw, dict):
                        cached_payload = dict(raw)

        if cached_checkout_url:
            return BillingCheckoutResponse(
                provider=provider_name,
                checkout_url=cached_checkout_url,
                checkout_payload=cached_payload,
                order_id=order_id,
                external_order_id=external_order_id,
            )

    provider_for_order = get_payment_provider(provider_name)
    metadata: Dict[str, Any] = {"order_id": order_id, "user_id": identity.username, "plan_code": plan_code}
    try:
        session_payload = await asyncio.to_thread(
            provider_for_order.create_checkout_session,
            user_id=identity.username,
            plan_code=plan_code,
            amount_cents=amount_cents,
            currency=currency,
            external_order_id=external_order_id,
            success_url=str(payload.success_url or ""),
            cancel_url=str(payload.cancel_url or ""),
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider=str(provider_name or provider_for_order.name),
                event_type="checkout.create",
                external_order_id=external_order_id,
                raw_payload=json.dumps({"user_id": identity.username, "plan_code": plan_code, "order_id": order_id}, ensure_ascii=False),
                signature=None,
                signature_valid=True,
                outcome="provider_error",
                detail=str(exc)[:800],
                occurred_at=now,
            )
        raise HTTPException(status_code=502, detail="payment provider error") from exc

    checkout_payload: Dict[str, Any] = dict(session_payload.raw or {})
    checkout_url = str(session_payload.checkout_url or "").strip()
    checkout_patch = {
        "checkout": {
            "provider": str(session_payload.provider),
            "checkout_url": checkout_url,
            "raw": checkout_payload,
            "created_at": now.isoformat(),
        }
    }
    with session_scope() as session:
        repo = BillingRepository(session)
        repo.update_order_provider_payload(order_id, patch=checkout_patch, now=now)
        repo.record_audit_log(
            provider=str(session_payload.provider),
            event_type="checkout.create",
            external_order_id=external_order_id,
            raw_payload=json.dumps({"user_id": identity.username, "plan_code": plan_code, "order_id": order_id}, ensure_ascii=False),
            signature=None,
            signature_valid=True,
            outcome="ok",
            detail=json.dumps(checkout_patch["checkout"], ensure_ascii=False),
            occurred_at=now,
        )

    return BillingCheckoutResponse(
        provider=str(session_payload.provider),
        checkout_url=checkout_url,
        checkout_payload=checkout_payload,
        order_id=order_id,
        external_order_id=external_order_id,
    )


@app.post("/billing/dev/simulate-payment", response_model=PaymentWebhookResponse)
async def billing_dev_simulate_payment(
    payload: BillingDevSimulatePaymentRequest,
    identity: AuthIdentity = Depends(get_current_identity),
) -> PaymentWebhookResponse:
    """
    Development-only helper to simulate a paid order.

    This is used by the web UI demo flow (QR modal) without requiring a real
    upstream payment gateway.
    """

    if str(APP_ENV or "").strip().lower() in {"prod", "production"}:
        raise HTTPException(status_code=404, detail="not found")

    external_order_id = payload.external_order_id.strip()
    if not external_order_id:
        raise HTTPException(status_code=400, detail="external_order_id is required")

    event_id = str(payload.event_id or f"dev_sim_{uuid.uuid4().hex[:20]}").strip()
    now = datetime.now(timezone.utc)
    user_id_for_cache_invalidate = ""

    with session_scope() as session:
        repo = BillingRepository(session)
        order = repo.get_order_by_external_order_id(external_order_id)
        if not order:
            raise HTTPException(status_code=404, detail=f"order not found: {external_order_id}")
        if (not _is_admin_role(identity)) and str(order.user_id) != str(identity.username):
            raise HTTPException(status_code=403, detail="not allowed")
        user_id_for_cache_invalidate = str(getattr(order, "user_id", "") or "")

        event = {
            "event_type": "payment.succeeded",
            "event_id": event_id,
            "provider": "dev-simulator",
            "data": {
                "external_order_id": external_order_id,
                "amount_cents": int(getattr(order, "amount_cents", 0) or 0),
                "currency": str(getattr(order, "currency", "cny") or "cny").lower(),
                "paid_at": now.isoformat(),
            },
        }
        raw_text = json.dumps(event, ensure_ascii=False)
        try:
            result = process_payment_webhook(repo, event)
            repo.record_audit_log(
                provider="dev",
                event_type="dev.simulate_payment",
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=None,
                signature_valid=True,
                outcome=str(result.get("status") or "processed"),
                detail=json.dumps(result, ensure_ascii=False),
                occurred_at=now,
            )
        except PaymentWebhookError as exc:
            repo.record_audit_log(
                provider="dev",
                event_type="dev.simulate_payment",
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=None,
                signature_valid=True,
                outcome="rejected_validation",
                detail=str(exc),
                occurred_at=now,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if user_id_for_cache_invalidate:
        invalidate_user_entitlements(user_id_for_cache_invalidate)

    return PaymentWebhookResponse(**result)


@app.post("/admin/billing/plans", response_model=BillingPlanResponse)
async def create_billing_plan(
    payload: BillingPlanCreateRequest,
    _: AuthIdentity = Depends(require_admin),
) -> BillingPlanResponse:
    with session_scope() as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(
            code=payload.code.strip(),
            name=payload.name.strip(),
            price_cents=payload.price_cents,
            monthly_points=payload.monthly_points,
            currency=payload.currency.strip().lower(),
            description=(payload.description or "").strip() or None,
            active=payload.active,
        )
        return _to_plan_response(plan)


@app.put("/admin/billing/plans/{plan_id}", response_model=BillingPlanResponse)
async def update_billing_plan(
    plan_id: str,
    payload: BillingPlanUpdateRequest,
    _: AuthIdentity = Depends(require_admin),
) -> BillingPlanResponse:
    with session_scope() as session:
        repo = BillingRepository(session)
        try:
            plan = repo.update_plan(
                plan_id,
                code=payload.code,
                name=payload.name,
                description=payload.description,
                currency=payload.currency,
                price_cents=payload.price_cents,
                monthly_points=payload.monthly_points,
                active=payload.active,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _to_plan_response(plan)


@app.get("/admin/saas/plans", response_model=List[SaaSPlanResponse])
async def saas_list_plans(_: AuthIdentity = Depends(require_admin)) -> List[SaaSPlanResponse]:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        plans = repo.list_plans(include_inactive=True)
        return [_to_saas_plan_response(item) for item in plans]


@app.post("/admin/saas/plans", response_model=SaaSPlanResponse)
async def saas_create_plan(
    payload: BillingPlanCreateRequest,
    _: AuthIdentity = Depends(require_admin),
) -> SaaSPlanResponse:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(
            code=payload.code.strip(),
            name=payload.name.strip(),
            price_cents=payload.price_cents,
            monthly_points=payload.monthly_points,
            currency=payload.currency.strip().lower(),
            description=(payload.description or "").strip() or None,
            active=payload.active,
            billing_cycle=(payload.billing_cycle or "monthly").strip().lower(),
            trial_days=int(payload.trial_days),
            metadata_json=dict(payload.metadata or {}),
        )
        return _to_saas_plan_response(plan)


@app.put("/admin/saas/plans/{plan_id}", response_model=SaaSPlanResponse)
async def saas_update_plan(
    plan_id: str,
    payload: BillingPlanUpdateRequest,
    _: AuthIdentity = Depends(require_admin),
) -> SaaSPlanResponse:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        try:
            plan = repo.update_plan(
                plan_id,
                code=payload.code,
                name=payload.name,
                description=payload.description,
                currency=payload.currency,
                price_cents=payload.price_cents,
                monthly_points=payload.monthly_points,
                active=payload.active,
                billing_cycle=payload.billing_cycle,
                trial_days=payload.trial_days,
                metadata_json=payload.metadata,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _to_saas_plan_response(plan)


@app.delete("/admin/saas/plans/{plan_id}", response_model=SaaSPlanResponse)
async def saas_deactivate_plan(plan_id: str, _: AuthIdentity = Depends(require_admin)) -> SaaSPlanResponse:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        try:
            plan = repo.deactivate_plan(plan_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    invalidate_plan_entitlements(plan_id)
    return _to_saas_plan_response(plan)


@app.get("/admin/saas/plans/{plan_id}/entitlements", response_model=List[BillingPlanEntitlementResponse])
async def saas_list_plan_entitlements(
    plan_id: str,
    include_disabled: bool = Query(True),
    _: AuthIdentity = Depends(require_admin),
) -> List[BillingPlanEntitlementResponse]:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        plan = repo.get_plan_by_id(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        items = repo.list_plan_entitlements(plan_id=plan_id, include_disabled=bool(include_disabled))
        return [_to_plan_entitlement_response(item) for item in items]


@app.post("/admin/saas/plans/{plan_id}/entitlements", response_model=BillingPlanEntitlementResponse)
async def saas_create_plan_entitlement(
    plan_id: str,
    payload: BillingPlanEntitlementCreateRequest,
    _: AuthIdentity = Depends(require_admin),
) -> BillingPlanEntitlementResponse:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        try:
            item = repo.create_plan_entitlement(
                plan_id=plan_id,
                key=payload.key,
                enabled=payload.enabled,
                value_json=payload.value,
                limit_value=payload.limit,
                metadata_json=payload.metadata,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    invalidate_plan_entitlements(plan_id)
    return _to_plan_entitlement_response(item)


@app.put("/admin/saas/entitlements/{entitlement_id}", response_model=BillingPlanEntitlementResponse)
async def saas_update_plan_entitlement(
    entitlement_id: str,
    payload: BillingPlanEntitlementUpdateRequest,
    _: AuthIdentity = Depends(require_admin),
) -> BillingPlanEntitlementResponse:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        patch = payload.model_dump(exclude_unset=True)
        kwargs: Dict[str, Any] = {}
        if "key" in patch:
            kwargs["key"] = patch.get("key")
        if "enabled" in patch:
            kwargs["enabled"] = patch.get("enabled")
        if "value" in payload.model_fields_set:
            kwargs["value_json"] = payload.value
        if "limit" in payload.model_fields_set:
            kwargs["limit_value"] = payload.limit
        if "metadata" in payload.model_fields_set:
            kwargs["metadata_json"] = payload.metadata
        try:
            item = repo.update_plan_entitlement(entitlement_id, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    invalidate_plan_entitlements(str(getattr(item, "plan_id", "")))
    return _to_plan_entitlement_response(item)


@app.delete("/admin/saas/entitlements/{entitlement_id}", response_model=Dict[str, Any])
async def saas_delete_plan_entitlement(
    entitlement_id: str,
    _: AuthIdentity = Depends(require_admin),
) -> Dict[str, Any]:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        item = repo.get_plan_entitlement(entitlement_id)
        if item is None:
            raise HTTPException(status_code=404, detail="plan entitlement not found")
        plan_id = str(getattr(item, "plan_id", "") or "")
        deleted = repo.delete_plan_entitlement(entitlement_id)
    invalidate_plan_entitlements(plan_id)
    return {"deleted": bool(deleted), "entitlement_id": entitlement_id}


@app.post("/admin/saas/users/{username}/plan", response_model=BillingSubscriptionSnapshot)
async def saas_bind_user_plan(
    username: str,
    payload: BillingBindUserPlanRequest,
    _: AuthIdentity = Depends(require_admin),
) -> BillingSubscriptionSnapshot:
    _ensure_feature_enabled(FEATURE_SAAS_ADMIN_API)
    with session_scope() as session:
        repo = BillingRepository(session)
        plan = None
        if payload.plan_id:
            plan = repo.get_plan_by_id(payload.plan_id)
        elif payload.plan_code:
            plan = repo.get_plan_by_code(payload.plan_code)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")

        duration_days = payload.duration_days if payload.duration_days is not None else _duration_days_from_plan(plan)
        try:
            sub = repo.bind_user_plan(
                user_id=str(username or "").strip(),
                plan_id=str(getattr(plan, "id", "")),
                duration_days=max(1, int(duration_days)),
                auto_renew=bool(payload.auto_renew),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        plan_code = str(getattr(plan, "code", "") or "") or None
        plan_name = str(getattr(plan, "name", "") or "") or None
    invalidate_user_entitlements(str(username or "").strip())
    return BillingSubscriptionSnapshot(
        subscription_id=str(getattr(sub, "id", "")),
        status=str(getattr(getattr(sub, "status", None), "value", getattr(sub, "status", "active"))),
        plan_code=plan_code,
        plan_name=plan_name,
        expires_at=(
            getattr(sub, "expires_at").isoformat()
            if getattr(sub, "expires_at", None) is not None
            else None
        ),
    )


@app.get("/admin/billing/users/status", response_model=List[AdminUserBillingStatusResponse])
async def list_admin_user_billing_status(
    username: str = Query("", description="optional exact username filter"),
    tenant_id: str = Query("", description="optional tenant filter (root only)"),
    include_inactive: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    identity: AuthIdentity = Depends(require_admin),
) -> List[AdminUserBillingStatusResponse]:
    with session_scope() as session:
        repo = BillingRepository(session)
        normalized_username = username.strip()
        normalized_tenant_id = tenant_id.strip() or None

        if _is_root_role(identity):
            if normalized_username:
                user = repo.get_auth_user(normalized_username)
                if not user:
                    return []
                if normalized_tenant_id and str(getattr(user, "tenant_id", "") or "").strip() != normalized_tenant_id:
                    return []
                if (not include_inactive) and not bool(getattr(user, "active", True)):
                    return []
                users = [user]
            else:
                users = repo.list_auth_users(
                    include_inactive=bool(include_inactive),
                    tenant_id=normalized_tenant_id,
                    limit=limit,
                    offset=offset,
                )
        else:
            actor_tenant_id = _identity_tenant_id(repo, identity)
            if not actor_tenant_id:
                return []
            if normalized_tenant_id and normalized_tenant_id != actor_tenant_id:
                raise HTTPException(status_code=403, detail="cross-tenant access denied")

            if normalized_username:
                user = repo.get_auth_user(normalized_username)
                if not user:
                    return []
                target_tenant_id = str(getattr(user, "tenant_id", "") or "").strip()
                if target_tenant_id != actor_tenant_id:
                    raise HTTPException(status_code=403, detail="cross-tenant access denied")
                if (not include_inactive) and not bool(getattr(user, "active", True)):
                    return []
                users = [user]
            else:
                users = repo.list_auth_users(
                    include_inactive=bool(include_inactive),
                    tenant_id=actor_tenant_id,
                    limit=limit,
                    offset=offset,
                )

        repo.expire_due_subscriptions()
        items: list[AdminUserBillingStatusResponse] = []
        for user in users:
            normalized_user_identity = AuthIdentity(
                username=str(user.username),
                role=_normalize_role_value(str(getattr(user, "role", "user"))),
                tenant_id=str(getattr(user, "tenant_id", "") or "").strip() or None,
            )
            user_info = _to_auth_user_info(normalized_user_identity, repo)
            subscription = repo.get_active_subscription(str(user.username))
            snapshot = BillingSubscriptionSnapshot(status="none")
            if subscription:
                plan = subscription.plan
                snapshot = BillingSubscriptionSnapshot(
                    subscription_id=str(getattr(subscription, "id", "")),
                    status=str(
                        getattr(getattr(subscription, "status", None), "value", getattr(subscription, "status", "none"))
                    ),
                    plan_code=getattr(plan, "code", None),
                    plan_name=getattr(plan, "name", None),
                    expires_at=subscription.expires_at.isoformat() if subscription.expires_at else None,
                )

            items.append(
                AdminUserBillingStatusResponse(
                    username=user_info.username,
                    role=user_info.role,
                    active=bool(getattr(user, "active", True)),
                    tenant_id=user_info.tenant_id,
                    tenant_code=user_info.tenant_code,
                    tenant_name=user_info.tenant_name,
                    subscription=snapshot,
                    points_balance=repo.get_user_point_balance(str(user.username)),
                )
            )

        return items


@app.get("/admin/billing/orders", response_model=List[BillingOrderResponse])
async def list_billing_orders(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str = Query("", description="optional filter by user_id"),
    status: str = Query("", description="optional filter by order status"),
    identity: AuthIdentity = Depends(require_admin),
) -> List[BillingOrderResponse]:
    with session_scope() as session:
        repo = BillingRepository(session)
        status_value = status.strip().lower() or None
        status_enum = None
        if status_value:
            try:
                # Avoid importing OrderStatus into main.py; compare by value.
                from billing.models import (
                    OrderStatus as _OrderStatus,  # local import to keep startup light
                )

                status_enum = _OrderStatus(status_value)
            except Exception:
                raise HTTPException(status_code=400, detail=f"invalid status: {status_value}")
        normalized_user = user_id.strip() or None
        if _is_root_role(identity):
            orders = repo.list_orders(limit=limit, offset=offset, user_id=normalized_user, status=status_enum)
        else:
            actor_tenant_id = _identity_tenant_id(repo, identity)
            if not actor_tenant_id:
                return []
            allowed_usernames = _tenant_usernames(repo, actor_tenant_id)
            if normalized_user and normalized_user not in allowed_usernames:
                raise HTTPException(status_code=403, detail="cross-tenant access denied")
            scoped_ids = [normalized_user] if normalized_user else sorted(allowed_usernames)
            orders = repo.list_orders(limit=limit, offset=offset, user_ids=scoped_ids, status=status_enum)
        return [_to_order_response(order) for order in orders]


@app.get("/admin/billing/audit", response_model=List[BillingAuditLogResponse])
async def list_billing_audit_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    provider: str = Query("", description="optional filter by provider"),
    external_order_id: str = Query("", description="optional filter by external_order_id"),
    outcome: str = Query("", description="optional filter by outcome"),
    identity: AuthIdentity = Depends(require_admin),
) -> List[BillingAuditLogResponse]:
    with session_scope() as session:
        repo = BillingRepository(session)
        provider_filter = provider.strip() or None
        external_order_filter = external_order_id.strip() or None
        outcome_filter = outcome.strip() or None

        if _is_root_role(identity):
            logs = repo.list_audit_logs(
                limit=limit,
                offset=offset,
                provider=provider_filter,
                external_order_id=external_order_filter,
                outcome=outcome_filter,
            )
        else:
            actor_tenant_id = _identity_tenant_id(repo, identity)
            if not actor_tenant_id:
                return []
            allowed_usernames = _tenant_usernames(repo, actor_tenant_id)
            if not allowed_usernames:
                return []
            tenant_orders = repo.list_orders(limit=200, offset=0, user_ids=sorted(allowed_usernames))
            allowed_external_ids = {
                str(getattr(item, "external_order_id", "") or "")
                for item in tenant_orders
                if str(getattr(item, "external_order_id", "") or "")
            }
            if external_order_filter and external_order_filter not in allowed_external_ids:
                raise HTTPException(status_code=403, detail="cross-tenant access denied")
            logs = repo.list_audit_logs(
                limit=limit,
                offset=offset,
                provider=provider_filter,
                external_order_id=external_order_filter,
                external_order_ids=None if external_order_filter else sorted(allowed_external_ids),
                outcome=outcome_filter,
            )
        return [_to_audit_log_response(log) for log in logs]


@app.get("/admin/billing/audit/{log_id}", response_model=BillingAuditLogDetailResponse)
async def get_billing_audit_log(
    log_id: str,
    identity: AuthIdentity = Depends(require_admin),
) -> BillingAuditLogDetailResponse:
    with session_scope() as session:
        repo = BillingRepository(session)
        log = repo.get_audit_log(log_id)
        if not log:
            raise HTTPException(status_code=404, detail="audit log not found")
        if not _is_root_role(identity):
            actor_tenant_id = _identity_tenant_id(repo, identity)
            if not actor_tenant_id:
                raise HTTPException(status_code=403, detail="tenant scoped access denied")
            allowed_usernames = _tenant_usernames(repo, actor_tenant_id)
            tenant_orders = repo.list_orders(limit=200, offset=0, user_ids=sorted(allowed_usernames))
            allowed_external_ids = {
                str(getattr(item, "external_order_id", "") or "")
                for item in tenant_orders
                if str(getattr(item, "external_order_id", "") or "")
            }
            if str(getattr(log, "external_order_id", "") or "") not in allowed_external_ids:
                raise HTTPException(status_code=403, detail="cross-tenant access denied")
        return _to_audit_log_detail_response(log)


@app.get("/billing/subscription/me", response_model=BillingSubscriptionSnapshot)
async def my_subscription(identity: AuthIdentity = Depends(get_current_identity)) -> BillingSubscriptionSnapshot:
    with session_scope() as session:
        repo = BillingRepository(session)
        repo.expire_due_subscriptions()
        subscription = repo.get_active_subscription(identity.username)
        if not subscription:
            return BillingSubscriptionSnapshot(status="none")
        plan = subscription.plan
        return BillingSubscriptionSnapshot(
            subscription_id=subscription.id,
            status=str(subscription.status.value if hasattr(subscription.status, "value") else subscription.status),
            plan_code=getattr(plan, "code", None),
            plan_name=getattr(plan, "name", None),
            expires_at=subscription.expires_at.isoformat() if subscription.expires_at else None,
        )


@app.get("/billing/points/me", response_model=BillingPointsSnapshot)
async def my_point_balance(identity: AuthIdentity = Depends(get_current_identity)) -> BillingPointsSnapshot:
    with session_scope() as session:
        repo = BillingRepository(session)
        balance = repo.get_user_point_balance(identity.username)
        return BillingPointsSnapshot(user_id=identity.username, balance=balance)


@app.get("/billing/orders/me/{external_order_id}/status", response_model=BillingMyOrderStatusResponse)
async def my_order_status(
    external_order_id: str,
    identity: AuthIdentity = Depends(get_current_identity),
) -> BillingMyOrderStatusResponse:
    key = str(external_order_id or "").strip()
    if not key:
        raise HTTPException(status_code=404, detail="order not found")
    with session_scope() as session:
        repo = BillingRepository(session)
        order = repo.get_order_by_external_order_id(key)
        if not order or str(getattr(order, "user_id", "")) != str(identity.username):
            raise HTTPException(status_code=404, detail="order not found")
        status_value = getattr(getattr(order, "status", None), "value", getattr(order, "status", "pending"))
        created_at = getattr(order, "created_at", None)
        paid_at = getattr(order, "paid_at", None)
        return BillingMyOrderStatusResponse(
            order_id=str(getattr(order, "id", "")),
            external_order_id=key,
            status=str(status_value or "pending"),
            plan_code=getattr(getattr(order, "plan", None), "code", None),
            amount_cents=int(getattr(order, "amount_cents", 0) or 0),
            currency=str(getattr(order, "currency", "cny") or "cny").lower(),
            created_at=created_at.isoformat() if created_at is not None else None,
            paid_at=paid_at.isoformat() if paid_at is not None else None,
        )


@app.get("/billing/points/history/me", response_model=List[BillingPointHistoryItem])
async def my_point_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: AuthIdentity = Depends(get_current_identity),
) -> List[BillingPointHistoryItem]:
    with session_scope() as session:
        repo = BillingRepository(session)
        flows = repo.list_point_flows(user_id=identity.username, limit=limit, offset=offset)
        items: list[BillingPointHistoryItem] = []
        for flow in flows:
            flow_type_value = getattr(getattr(flow, "flow_type", None), "value", getattr(flow, "flow_type", ""))
            occurred_at = getattr(flow, "occurred_at", None)
            items.append(
                BillingPointHistoryItem(
                    flow_id=str(getattr(flow, "id", "")),
                    flow_type=str(flow_type_value or ""),
                    points=int(getattr(flow, "points", 0) or 0),
                    balance_after=(
                        int(getattr(flow, "balance_after", 0))
                        if getattr(flow, "balance_after", None) is not None
                        else None
                    ),
                    note=str(getattr(flow, "note", "") or "").strip() or None,
                    order_id=str(getattr(flow, "order_id", "") or "").strip() or None,
                    subscription_id=str(getattr(flow, "subscription_id", "") or "").strip() or None,
                    occurred_at=occurred_at.isoformat() if occurred_at is not None else None,
                )
            )
        return items


@app.post("/billing/webhooks/payment", response_model=PaymentWebhookResponse)
async def payment_webhook(request: Request) -> PaymentWebhookResponse:
    if not PAYMENT_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="PAYMENT_WEBHOOK_SECRET is missing")
    signature = request.headers.get("X-Signature") or request.headers.get("X-Webhook-Signature") or ""
    raw = await request.body()
    if not verify_webhook_signature(raw, signature, PAYMENT_WEBHOOK_SECRET):
        raw_text = raw.decode("utf-8", errors="replace")
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="internal",
                event_type="payment.webhook",
                external_event_id=None,
                external_order_id=None,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=False,
                outcome="rejected_signature",
                detail="invalid webhook signature",
            )
        raise HTTPException(status_code=403, detail="invalid webhook signature")
    raw_text = raw.decode("utf-8", errors="replace")
    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="internal",
                event_type="payment.webhook",
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="rejected_payload",
                detail=str(exc),
            )
        raise HTTPException(status_code=400, detail=f"invalid webhook payload: {exc}") from exc
    event_dict = event if isinstance(event, dict) else {}
    provider = str(event_dict.get("provider") or "internal").strip() or "internal"
    event_type = str(event_dict.get("event_type") or event_dict.get("type") or "payment.webhook").strip() or "payment.webhook"
    event_id = str(event_dict.get("event_id") or event_dict.get("id") or event_dict.get("idempotency_key") or "").strip() or None
    data = event_dict.get("data") if isinstance(event_dict.get("data"), dict) else event_dict
    external_order_id = str(data.get("external_order_id") or data.get("order_id") or "").strip() or None
    user_id_for_cache_invalidate = ""

    try:
        with session_scope() as session:
            repo = BillingRepository(session)
            result = process_payment_webhook(repo, event_dict)
            if external_order_id:
                order = repo.get_order_by_external_order_id(external_order_id)
                if order is not None:
                    user_id_for_cache_invalidate = str(getattr(order, "user_id", "") or "")
            repo.record_audit_log(
                provider=provider,
                event_type=event_type,
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome=str(result.get("status") or "processed"),
                detail=json.dumps(result, ensure_ascii=False),
            )
    except PaymentWebhookError as exc:
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider=provider,
                event_type=event_type,
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="rejected_validation",
                detail=str(exc),
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider=provider,
                event_type=event_type,
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="error",
                detail=str(exc),
            )
        raise
    if user_id_for_cache_invalidate:
        invalidate_user_entitlements(user_id_for_cache_invalidate)
    return PaymentWebhookResponse(**result)


@app.post("/billing/webhooks/wechatpay")
async def wechatpay_webhook(request: Request) -> JSONResponse:
    """
    WeChat Pay v3 payment notification endpoint.

    This endpoint is public and must perform strict signature verification (403 on invalid signature),
    then AES-GCM decrypt the resource using WECHATPAY_APIV3_KEY.
    """

    if not WECHATPAY_APIV3_KEY:
        raise HTTPException(status_code=503, detail="WECHATPAY_APIV3_KEY is missing")

    try:
        platform_certs = parse_platform_certs(
            certs_json=WECHATPAY_PLATFORM_CERTS_JSON,
            cert_serial=WECHATPAY_PLATFORM_CERT_SERIAL,
            cert_pem=WECHATPAY_PLATFORM_CERT_PEM,
            cert_path=WECHATPAY_PLATFORM_CERT_PATH,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"wechatpay platform cert config error: {exc}") from exc

    if not platform_certs:
        raise HTTPException(status_code=503, detail="wechatpay platform certificate is missing")

    timestamp = str(request.headers.get("Wechatpay-Timestamp") or "").strip()
    nonce = str(request.headers.get("Wechatpay-Nonce") or "").strip()
    signature = str(request.headers.get("Wechatpay-Signature") or "").strip()
    serial = str(request.headers.get("Wechatpay-Serial") or "").strip()

    raw = await request.body()
    raw_text = raw.decode("utf-8", errors="replace")

    signature_valid = verify_wechatpay_notify_signature(
        timestamp=timestamp,
        nonce=nonce,
        signature_b64=signature,
        serial=serial,
        body=raw,
        platform_certs=platform_certs,
    )
    if not signature_valid:
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="wechatpay",
                event_type="wechatpay.notify",
                raw_payload=raw_text,
                signature=signature,
                signature_valid=False,
                outcome="rejected_signature",
                detail="invalid wechatpay signature",
            )
        return JSONResponse(status_code=403, content={"code": "FAIL", "message": "invalid signature"})

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="wechatpay",
                event_type="wechatpay.notify",
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="rejected_payload",
                detail=str(exc),
            )
        return JSONResponse(status_code=400, content={"code": "FAIL", "message": f"invalid payload: {exc}"})

    body_dict = body if isinstance(body, dict) else {}
    event_type = str(body_dict.get("event_type") or "wechatpay.notify").strip() or "wechatpay.notify"
    event_id = str(body_dict.get("id") or "").strip() or None

    resource = body_dict.get("resource") if isinstance(body_dict.get("resource"), dict) else {}
    try:
        tx = decrypt_notification(api_v3_key=WECHATPAY_APIV3_KEY, resource=resource)
    except Exception as exc:  # noqa: BLE001
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="wechatpay",
                event_type=event_type,
                external_event_id=event_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="rejected_payload",
                detail=f"decrypt failed: {exc}",
            )
        return JSONResponse(status_code=400, content={"code": "FAIL", "message": "decrypt failed"})

    external_order_id = str(tx.get("out_trade_no") or "").strip() or None
    trade_state = str(tx.get("trade_state") or "").strip().upper()
    amount = tx.get("amount") if isinstance(tx.get("amount"), dict) else {}
    total = amount.get("total")
    currency = str(amount.get("currency") or "CNY").strip().lower()
    success_time = tx.get("success_time")

    if not external_order_id:
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="wechatpay",
                event_type=event_type,
                external_event_id=event_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="rejected_validation",
                detail="missing out_trade_no",
            )
        return JSONResponse(status_code=400, content={"code": "FAIL", "message": "missing out_trade_no"})

    # Only SUCCESS is handled as paid for AntiHub's commercial scope.
    if trade_state != "SUCCESS":
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="wechatpay",
                event_type=event_type,
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="ignored",
                detail=f"trade_state={trade_state}",
            )
        return JSONResponse(status_code=200, content={"code": "SUCCESS", "message": "ignored"})

    event = {
        "event_type": "payment.succeeded",
        "event_id": event_id or str(tx.get("transaction_id") or f"wechatpay:{external_order_id}"),
        "provider": "wechatpay",
        "data": {
            "external_order_id": external_order_id,
            "amount_cents": total,
            "currency": currency,
            "paid_at": success_time,
        },
    }
    user_id_for_cache_invalidate = ""

    try:
        with session_scope() as session:
            repo = BillingRepository(session)
            result = process_payment_webhook(repo, event)
            order = repo.get_order_by_external_order_id(str(external_order_id))
            if order is not None:
                user_id_for_cache_invalidate = str(getattr(order, "user_id", "") or "")
            repo.record_audit_log(
                provider="wechatpay",
                event_type=event_type,
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome=str(result.get("status") or "processed"),
                detail=json.dumps(result, ensure_ascii=False),
            )
    except PaymentWebhookError as exc:
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="wechatpay",
                event_type=event_type,
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="rejected_validation",
                detail=str(exc),
            )
        return JSONResponse(status_code=400, content={"code": "FAIL", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001
        with session_scope() as session:
            repo = BillingRepository(session)
            repo.record_audit_log(
                provider="wechatpay",
                event_type=event_type,
                external_event_id=event_id,
                external_order_id=external_order_id,
                raw_payload=raw_text,
                signature=signature,
                signature_valid=True,
                outcome="error",
                detail=str(exc),
            )
        raise

    if user_id_for_cache_invalidate:
        invalidate_user_entitlements(user_id_for_cache_invalidate)
    return JSONResponse(status_code=200, content={"code": "SUCCESS", "message": "SUCCESS"})


@app.post("/admin/billing/subscriptions/expire", response_model=BillingExpireSubscriptionsResponse)
async def expire_subscriptions(_: AuthIdentity = Depends(require_admin)) -> BillingExpireSubscriptionsResponse:
    with session_scope() as session:
        repo = BillingRepository(session)
        expired = repo.expire_due_subscriptions()
    return BillingExpireSubscriptionsResponse(expired=expired)


@app.post("/recommendations", response_model=RecommendationResponse)
async def recommend_repos(
    query: str = Form(""),
    mode: str = Form("quick"),
    limit: int = Form(10),
    file: UploadFile | None = File(default=None),
    identity: AuthIdentity = Depends(get_current_identity),
) -> RecommendationResponse:
    query_value, mode_value, limit_value, requirement_text, warnings = await _prepare_recommendation_input(
        query=query,
        mode=mode,
        limit=limit,
        file=file,
    )
    _charge_deep_search_points_if_needed(identity, mode=mode_value, query=query_value or requirement_text)
    response = recommend_products(
        query=query_value,
        requirement_text=requirement_text,
        mode=mode_value,
        limit=limit_value,
    )
    if warnings:
        response.warnings.extend(warnings)
    return response


def _build_recommendation_thoughts(query: str, *, mode: str, has_upload: bool) -> list[str]:
    focus = query.strip() or "uploaded requirement"
    thoughts = [
        f"正在解析需求关键词与约束：{focus[:48]}...",
        "正在准备多源检索策略（GitHub/Gitee/GitCode/目录库）...",
        "正在执行关键词优先评分与语义护栏...",
    ]
    if has_upload:
        thoughts.insert(1, "正在抽取上传文档中的需求信号...")
    if mode.lower() == "deep":
        thoughts.append("正在执行深度聚合提炼与引文生成...")
    return thoughts


def _has_fast_mode_warning(warnings: list[str]) -> bool:
    return any("极速模式" in str(item or "") for item in warnings)


def _encode_stream_event(event_type: str, **payload: Any) -> bytes:
    body = {"type": event_type, **payload}
    return f"{json.dumps(body, ensure_ascii=False)}\n".encode("utf-8")


async def _prepare_recommendation_input(
    *,
    query: str,
    mode: str,
    limit: int,
    file: UploadFile | None,
) -> tuple[str, str, int, str, list[str]]:
    if not query and not file:
        raise HTTPException(status_code=400, detail="缺少需求描述或文件")
    try:
        query_value = _require_safe_input("query", str(query or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    mode_value = str(mode or "quick").strip() or "quick"
    # Business requirement: recommendation/search result set should not be smaller than 10.
    limit_value = max(10, min(int(limit), 20))
    requirement_text = ""
    warnings: list[str] = []
    if file:
        raw = await file.read()
        if RECOMMEND_MAX_UPLOAD_BYTES and len(raw) > RECOMMEND_MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="需求文件过大")
        requirement_text, warning = extract_text_from_upload(file.filename, raw)
        try:
            _require_safe_input("requirement_text", requirement_text[:4000])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if warning:
            warnings.append(warning)
    return query_value, mode_value, limit_value, requirement_text, warnings


def _charge_deep_search_points_if_needed(identity: Any, *, mode: str, query: str) -> None:
    if not isinstance(identity, AuthIdentity):
        return
    if not is_deep_search_mode(mode):
        return
    required_points = int(DEEP_SEARCH_POINTS_COST or 0)
    if required_points <= 0:
        return
    with session_scope() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="billing service unavailable")
        billing_repo = BillingRepository(session)
        note_query = str(query or "").strip().replace("\n", " ")[:80]
        note = f"deep_search:{note_query}" if note_query else "deep_search"
        try:
            billing_repo.consume_points(
                user_id=identity.username,
                points=required_points,
                idempotency_key=f"deep-search:{identity.username}:{uuid.uuid4().hex}",
                note=note,
            )
        except BillingStateError as exc:
            balance = billing_repo.get_user_point_balance(identity.username)
            detail = str(exc or "").strip()
            if "insufficient points" in detail.lower():
                raise HTTPException(
                    status_code=402,
                    detail=f"积分不足：深度搜索需要 {required_points} 积分，当前余额 {balance}。",
                ) from exc
            raise HTTPException(status_code=409, detail=f"积分扣减冲突，请重试。当前余额 {balance}。") from exc


@app.post("/recommendations/stream")
async def recommend_repos_stream(
    query: str = Form(""),
    mode: str = Form("quick"),
    limit: int = Form(10),
    file: UploadFile | None = File(default=None),
    identity: AuthIdentity = Depends(get_current_identity),
) -> StreamingResponse:
    query_value, mode_value, limit_value, requirement_text, warnings = await _prepare_recommendation_input(
        query=query,
        mode=mode,
        limit=limit,
        file=file,
    )
    _charge_deep_search_points_if_needed(identity, mode=mode_value, query=query_value or requirement_text)
    thought_steps = _build_recommendation_thoughts(query_value, mode=mode_value, has_upload=bool(file))

    async def stream() -> AsyncIterator[bytes]:
        for thought in thought_steps:
            yield _encode_stream_event("thought", message=thought)
            await asyncio.sleep(0.12)

        progress_queue: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _on_progress(message: str) -> None:
            text = str(message or "").strip()
            if not text:
                return
            loop.call_soon_threadsafe(progress_queue.put_nowait, text)

        task = asyncio.create_task(
            asyncio.to_thread(
                recommend_products,
                query=query_value,
                requirement_text=requirement_text,
                mode=mode_value,
                limit=limit_value,
                progress_callback=_on_progress,
            )
        )

        try:
            while True:
                if task.done() and progress_queue.empty():
                    break
                try:
                    progress_line = await asyncio.wait_for(progress_queue.get(), timeout=0.16)
                except asyncio.TimeoutError:
                    continue
                yield _encode_stream_event("thought", message=progress_line)

            response = await task
            if warnings:
                response.warnings.extend(warnings)
            if _has_fast_mode_warning(response.warnings):
                yield _encode_stream_event("thought", message="AI 服务繁忙，已切换至极速模式（关键词检索）。")
        except Exception as exc:  # noqa: BLE001
            yield _encode_stream_event("error", message=f"recommendation failed: {exc}")
            return
        yield _encode_stream_event("result", data=response.model_dump(mode="json"))

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/products/{product_id}/deploy", response_model=ProductDeployActionResponse)
async def resolve_product_deploy_action(
    product_id: str,
    identity: AuthIdentity = Depends(get_current_identity),
) -> ProductDeployActionResponse:
    try:
        action = resolve_product_action(case_id=str(product_id or "").strip())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    detail = action.detail
    if action.deploy_supported and ONE_CLICK_DEPLOY_POINTS_COST > 0:
        with session_scope() as session:
            if session is not None:
                repo = BillingRepository(session)
                balance = repo.get_user_point_balance(identity.username)
                detail = (
                    f"{detail or '支持一键部署。'}"
                    f" 启动部署将扣除 {ONE_CLICK_DEPLOY_POINTS_COST} 积分（当前余额 {balance}）。"
                )
    return ProductDeployActionResponse(
        product_id=product_id,
        action_type=action.action_type,
        label=action.label,
        url=action.url,
        deploy_supported=bool(action.deploy_supported),
        detail=detail,
    )


@app.get("/error-codes")
async def error_codes() -> Dict[str, Dict[str, str]]:
    return ERROR_CODE_MAP


@app.get("/stats")
async def stats() -> Dict[str, Any]:
    return {"manual": get_manual_stats()}


@app.get("/case-templates", response_model=List[CaseTemplate])
async def case_templates() -> List[CaseTemplate]:
    templates: List[CaseTemplate] = []
    raw_templates = load_templates()
    for item in raw_templates:
        if not isinstance(item, dict):
            continue
        payload = dict(item)
        if not payload.get("repo_url") and payload.get("git_url"):
            payload["repo_url"] = payload.get("git_url")
        ref_value = payload.get("default_ref") or payload.get("ref")
        if ref_value is not None:
            payload["ref"] = str(ref_value).strip()
        if payload.get("default_mode") is None and payload.get("mode") is not None:
            payload["default_mode"] = payload.get("mode")
        env_keys = payload.get("suggested_env_keys") or payload.get("default_env_keys") or []
        if not isinstance(env_keys, list):
            env_keys = []
        payload["default_env_keys"] = [str(key) for key in env_keys]
        expected = payload.get("expected")
        if expected is not None and not isinstance(expected, dict):
            payload["expected"] = None
        dimensions = payload.get("dimensions") or []
        if not isinstance(dimensions, list):
            dimensions = []
        payload["dimensions"] = [str(item) for item in dimensions if str(item).strip()]
        if "what_to_verify" in payload:
            payload["what_to_verify"] = str(payload.get("what_to_verify") or "").strip() or None
        if not payload.get("name"):
            continue
        if not payload.get("repo_url"):
            continue
        templates.append(CaseTemplate(**payload))
    return templates


def _load_plans() -> List[PlanInfo]:
    raw_plans = PLANS if isinstance(PLANS, list) else []
    if not raw_plans:
        raw_plans = [
            {
                "plan_id": "free",
                "name": "Free",
                "description": "Starter plan for evaluation.",
                "limits": {
                    "max_concurrent_cases": 1,
                    "daily_cases": 5,
                    "timeout_build_seconds": 900,
                },
            },
            {
                "plan_id": "pro",
                "name": "Pro",
                "description": "Production-ready defaults.",
                "limits": {
                    "max_concurrent_cases": 3,
                    "daily_cases": 50,
                    "timeout_build_seconds": 1800,
                },
            },
        ]
    plans: List[PlanInfo] = []
    for item in raw_plans:
        if not isinstance(item, dict):
            continue
        plan_id = str(item.get("plan_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not plan_id or not name:
            continue
        plans.append(
            PlanInfo(
                plan_id=plan_id,
                name=name,
                description=str(item.get("description") or "").strip() or None,
                limits=item.get("limits") or {},
                notes=str(item.get("notes") or "").strip() or None,
            )
        )
    return plans


@app.get("/templates", response_model=List[TemplateInfo])
async def templates() -> List[TemplateInfo]:
    items = load_templates()
    return [TemplateInfo(**item) for item in items]


@app.get("/templates/{template_id}", response_model=TemplateInfo)
async def template_detail(template_id: str) -> TemplateInfo:
    item = get_template(template_id)
    if not item:
        raise HTTPException(status_code=404, detail="template not found")
    return TemplateInfo(**item)


@app.get("/plans", response_model=List[PlanInfo])
async def plans() -> List[PlanInfo]:
    return _load_plans()


@app.get("/usage", response_model=UsageInfo)
async def usage() -> UsageInfo:
    import datetime as _dt

    ids = list_case_ids()
    total_cases = 0
    running_cases = 0
    today_cases = 0
    today = _dt.date.today()
    start_ts = _dt.datetime.combine(today, _dt.time.min).timestamp()
    running_statuses = {"CLONING", "BUILDING", "STARTING", "RUNNING", "ANALYZING"}
    for case_id in ids:
        data = get_case(case_id) or {}
        if not data:
            continue
        total_cases += 1
        status = str(data.get("status") or "").upper()
        if status in running_statuses:
            running_cases += 1
        created_at = data.get("created_at")
        if isinstance(created_at, (int, float)) and created_at >= start_ts:
            today_cases += 1
    return UsageInfo(
        date=str(today),
        total_cases=total_cases,
        running_cases=running_cases,
        today_cases=today_cases,
    )


@app.get("/cases", response_model=CaseListResponse)
async def list_cases(
    request: Request,
    q: Optional[str] = Query(None, description="Search by case_id/repo/ref"),
    search: Optional[str] = Query(None, description="Alias for q (search keyword)"),
    status: Optional[str] = Query(None, description="Filter by status"),
    stage: Optional[str] = Query(None, description="Filter by stage"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=200, description="Page size"),
    limit: Optional[int] = Query(None, ge=1, le=200, description="Alias for size"),
    offset: Optional[int] = Query(None, ge=0, description="Alias for page offset"),
    include_archived: bool = Query(False, description="Include archived cases"),
    tenant_id: str = Query("", description="Optional tenant filter (root only)"),
) -> CaseListResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    requested_tenant_id = str(tenant_id or "").strip() or None
    if not _is_root_role(identity):
        if requested_tenant_id and actor_tenant_id and requested_tenant_id != actor_tenant_id:
            raise HTTPException(status_code=403, detail="cross-tenant access denied")
        requested_tenant_id = actor_tenant_id

    if search and not q:
        q = search
    if limit is not None:
        size = limit
    if offset is not None and size > 0:
        page = (offset // size) + 1
    ids = list_case_ids()
    items: List[CaseResponse] = []
    for case_id in ids:
        data = get_case(case_id) or {}
        if not data:
            continue
        if not _is_case_visible_to_identity(identity, data, actor_tenant_id=actor_tenant_id):
            continue
        case_tenant_id = _case_tenant_id(data)
        if requested_tenant_id and case_tenant_id != requested_tenant_id:
            continue
        if not include_archived and bool(data.get("archived", False)):
            continue
        if status and (data.get("status") or "").upper() != status.upper():
            continue
        normalized_stage = normalize_stage(data.get("stage"), data.get("status"))
        if stage and normalized_stage != stage.lower():
            continue
        if q and not matches_query(case_id, data, q):
            continue
        items.append(build_case_response(case_id, data))
    items.sort(key=lambda item: item.updated_at or item.created_at or 0, reverse=True)
    total = len(items)
    start = (page - 1) * size
    end = start + size
    return CaseListResponse(items=items[start:end], total=total, page=page, size=size)


async def create_case(payload: CaseCreateRequest, request: Request | None = None) -> CaseResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    requested_tenant_id = str(payload.tenant_id or "").strip() or None
    if _is_root_role(identity):
        target_tenant_id = requested_tenant_id or actor_tenant_id
    else:
        if requested_tenant_id and actor_tenant_id and requested_tenant_id != actor_tenant_id:
            raise HTTPException(status_code=403, detail="cross-tenant access denied")
        if not actor_tenant_id:
            raise HTTPException(status_code=403, detail="tenant scoped access denied")
        target_tenant_id = actor_tenant_id

    if target_tenant_id:
        with session_scope() as session:
            if session is not None:
                repo = BillingRepository(session)
                if repo.get_tenant_by_id(target_tenant_id) is None:
                    raise HTTPException(status_code=404, detail="tenant not found")

    template_payload = None
    if payload.template_id:
        template_payload = get_template(payload.template_id)
        if not template_payload:
            raise HTTPException(status_code=404, detail="template_id not found")
    repo_url = payload.repo_url or payload.git_url
    if not repo_url and template_payload:
        repo_url = template_payload.get("repo_url")
    if not repo_url:
        raise HTTPException(status_code=422, detail="git_url or repo_url is required")
    ref = payload.ref or payload.branch
    if ref is not None:
        ref = ref.strip()
        if not ref:
            ref = None
    if not ref and template_payload:
        ref = str(template_payload.get("default_ref") or "").strip() or None
    dockerfile_path = (payload.dockerfile_path or "").strip() or None
    context_path = (payload.context_path or "").strip() or None
    compose_file = (payload.compose_file or "").strip() or None
    if "dockerfile_path" not in payload.model_fields_set:
        dockerfile_path = None
        if template_payload:
            dockerfile_path = str(template_payload.get("dockerfile_path") or "").strip() or None
    if "context_path" not in payload.model_fields_set:
        context_path = None
        if template_payload:
            context_path = str(template_payload.get("context_path") or "").strip() or None
    if "compose_file" not in payload.model_fields_set:
        compose_file = None
        if template_payload:
            compose_file = str(template_payload.get("compose_file") or "").strip() or None
    if not compose_file and "frappe_docker" in str(repo_url):
        compose_file = "pwd.yml"
    raw_mode = None
    if "run_mode" in payload.model_fields_set:
        raw_mode = payload.run_mode
    elif "mode" in payload.model_fields_set:
        raw_mode = payload.mode
    if raw_mode is None and template_payload:
        raw_mode = template_payload.get("default_mode")
    auto_mode = bool(payload.auto_mode)
    run_mode = normalize_run_mode(raw_mode, None, auto_mode)
    mode = run_mode
    auto_manual = payload.auto_manual if payload.auto_manual is not None else True
    build_payload = payload.build or BuildOptions()
    git_payload = payload.git or GitOptions()
    build_network = build_payload.network or payload.docker_build_network
    build_no_cache = build_payload.no_cache if build_payload.no_cache is not None else payload.docker_no_cache
    build_use_buildkit = build_payload.use_buildkit
    build_args = build_payload.build_args or payload.docker_build_args or {}
    git_submodules = (
        git_payload.enable_submodule
        if git_payload.enable_submodule is not None
        else payload.enable_submodule
        if payload.enable_submodule is not None
        else payload.enable_submodules
    )
    git_lfs = git_payload.enable_lfs if git_payload.enable_lfs is not None else payload.enable_lfs
    case_id = f"c_{uuid.uuid4().hex[:6]}"
    one_click_requested = bool(payload.one_click_deploy)
    if one_click_requested and not _run_mode_requires_runtime(run_mode):
        raise HTTPException(status_code=422, detail="one_click_deploy requires deploy runtime mode")
    deploy_points_cost = 0
    if one_click_requested and ONE_CLICK_DEPLOY_POINTS_COST > 0:
        with session_scope() as session:
            if session is None:
                raise HTTPException(status_code=503, detail="billing service unavailable")
            billing_repo = BillingRepository(session)
            required_points = int(ONE_CLICK_DEPLOY_POINTS_COST)
            balance = billing_repo.get_user_point_balance(identity.username)
            if balance < required_points:
                raise HTTPException(
                    status_code=402,
                    detail=f"积分不足：一键部署需要 {required_points} 积分，当前余额 {balance}。",
                )
            try:
                point_flow = billing_repo.consume_points(
                    user_id=identity.username,
                    points=required_points,
                    idempotency_key=f"deploy:{identity.username}:{case_id}",
                    note=f"one_click_deploy:{case_id}",
                )
            except BillingStateError as exc:
                raise HTTPException(status_code=402, detail=f"积分扣减失败：{exc}") from exc
            deploy_points_cost = abs(int(point_flow.points or 0))
    now = time.time()
    env_keys = sorted(list((payload.env or {}).keys()))
    if template_payload:
        for key in template_payload.get("suggested_env_keys") or []:
            if key not in env_keys:
                env_keys.append(key)
        env_keys = sorted(env_keys)
    build_arg_keys = sorted(list((build_args or {}).keys()))
    data: Dict[str, Any] = {
        "case_id": case_id,
        "tenant_id": target_tenant_id,
        "owner_username": identity.username,
        "status": "PENDING",
        "stage": "system",
        "mode": mode,
        "run_mode": run_mode,
        "one_click_deploy": one_click_requested,
        "deploy_points_cost": deploy_points_cost,
        "auto_mode": auto_mode,
        "auto_manual": auto_manual,
        "repo_url": repo_url,
        "ref": ref,
        "branch": ref,
        "resolved_ref": None,
        "container_port": payload.container_port,
        "dockerfile_path": dockerfile_path,
        "compose_file": compose_file,
        "context_path": context_path,
        "resolved_dockerfile_path": None,
        "resolved_context_path": None,
        "preflight_meta": None,
        "docker_build_network": build_network,
        "docker_no_cache": build_no_cache,
        "docker_buildkit": build_use_buildkit,
        "build_arg_keys": build_arg_keys,
        "git_submodules": git_submodules,
        "git_lfs": git_lfs,
        "env_keys": env_keys,
        "report_ready": False,
        "report_cached": False,
        "analyze_status": "PENDING",
        "analyze_error_code": None,
        "analyze_error_message": None,
        "visual_ready": False,
        "visual_cached": False,
        "visual_status": "NOT_STARTED",
        "visual_error_code": None,
        "visual_error_message": None,
        "created_at": now,
        "updated_at": now,
        "archived": False,
        "archived_at": None,
        "attempt": 1,
        "retry_of": None,
        "manual_status": "NOT_STARTED",
        "manual_generated_at": None,
        "manual_error_code": None,
        "manual_error_message": None,
        "runtime": {
            "container_id": None,
            "host_port": None,
            "access_url": None,
            "started_at": None,
            "exited_at": None,
            "exit_code": None,
        },
    }
    set_case(case_id, data)
    build_and_run.delay(
        case_id,
        repo_url,
        ref,
        payload.container_port,
        payload.env,
        payload.auto_analyze,
        dockerfile_path,
        compose_file,
        context_path,
        build_network,
        build_no_cache,
        build_args,
        git_submodules,
        git_lfs,
        mode,
        auto_mode,
        auto_manual,
        build_use_buildkit,
    )
    return build_case_response(case_id, data)


@app.post("/cases", response_model=CaseResponse)
async def create_case_api(payload: CaseCreateRequest, request: Request) -> CaseResponse:
    return await create_case(payload, request)


@app.get("/cases/{case_id}", response_model=CaseResponse)
async def get_case_status(case_id: str, request: Request) -> CaseResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    data.pop("case_id", None)
    return build_case_response(case_id, data)


@app.get("/cases/{case_id}/open/erpnext")
async def open_case_erpnext(case_id: str, request: Request) -> RedirectResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    status = (data.get("status") or "").upper()
    if status != "RUNNING":
        raise HTTPException(status_code=409, detail="Case not running")

    runtime = data.get("runtime") or {}
    if not isinstance(runtime, dict):
        runtime = {}
    access_url = runtime.get("access_url") or data.get("access_url")
    host_port = runtime.get("host_port") or data.get("host_port")
    ports = runtime.get("ports") or []

    target_url = None
    if access_url and ":8080" in str(access_url):
        target_url = access_url
    elif host_port == 8080 or (isinstance(ports, list) and 8080 in ports):
        target_url = access_url or f"http://{PUBLIC_HOST}:8080"

    if not target_url:
        raise HTTPException(status_code=404, detail="ERPNext not available for this case")
    return RedirectResponse(url=target_url, status_code=302)


def handle_case_action(
    case_id: str,
    action: str,
    identity: AuthIdentity,
    env: Optional[Dict[str, str]] = None,
    build_args: Optional[Dict[str, str]] = None,
) -> CaseActionResponse:
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    action_key = action.lower()
    runtime = data.get("runtime") or {}
    container_id = data.get("container_id") or runtime.get("container_id")
    host_port = data.get("host_port") or runtime.get("host_port")
    compose_project = data.get("compose_project_name")
    compose_file = data.get("compose_file")
    repo_dir = data.get("repo_dir")

    if action_key == "stop":
        if compose_project:
            run_compose_down(case_id, repo_dir, compose_file, compose_project)
            update_case(
                case_id,
                {
                    "status": "STOPPED",
                    "stage": "run",
                    "error_code": "STOPPED_BY_USER",
                    "error_message": "Stopped by user",
                    "runtime": {
                        **runtime,
                        "exited_at": time.time(),
                        "exit_code": 0,
                    },
                },
            )
            append_system_log(case_id, "Compose stack stopped by user")
            return CaseActionResponse(case_id=case_id, action=action_key, status="STOPPED", message="Stopped")
        if not container_id:
            raise HTTPException(status_code=400, detail="No running container for case")
        container = get_managed_container(case_id, container_id)
        try:
            container.stop(timeout=10)
        except Exception:
            container.kill()
        container.reload()
        exit_code = container.attrs.get("State", {}).get("ExitCode")
        update_case(
            case_id,
            {
                "status": "STOPPED",
                "stage": "run",
                "error_code": "STOPPED_BY_USER",
                "error_message": "Stopped by user",
                "runtime": {
                    **runtime,
                    "exited_at": time.time(),
                    "exit_code": exit_code,
                },
            },
        )
        append_system_log(case_id, "Container stopped by user")
        if PORT_MODE != "dynamic":
            release_port(host_port)
        return CaseActionResponse(case_id=case_id, action=action_key, status="STOPPED", message="Stopped")

    if action_key == "restart":
        if not container_id:
            raise HTTPException(status_code=400, detail="No existing container to restart")
        container = get_managed_container(case_id, container_id)
        if PORT_MODE != "dynamic" and host_port:
            try:
                reserve_specific_port(case_id, int(host_port))
            except BuildError as exc:
                raise HTTPException(status_code=409, detail=f"{exc.code}: {exc}") from exc
        container.start()
        wait_for_container_running(docker.from_env(), container_id, STARTUP_TIMEOUT_SECONDS)
        container.reload()
        container_port = data.get("container_port")
        host_port = host_port or resolve_host_port(container, container_port)
        access_url = data.get("access_url") or runtime.get("access_url")
        if host_port and not access_url:
            access_url = f"http://{PUBLIC_HOST}:{host_port}"
        update_case(
            case_id,
            {
                "status": "RUNNING",
                "stage": "run",
                "error_code": None,
                "error_message": None,
                "container_id": container_id,
                "host_port": host_port,
                "access_url": access_url,
                "runtime": {
                    **runtime,
                    "container_id": container_id,
                    "host_port": host_port,
                    "access_url": access_url,
                    "started_at": time.time(),
                    "exited_at": None,
                    "exit_code": None,
                },
            },
        )
        append_system_log(case_id, "Container restarted")
        return CaseActionResponse(case_id=case_id, action=action_key, status="RUNNING", message="Restarted")

    if action_key == "retry":
        repo_url = data.get("repo_url")
        if not repo_url:
            raise HTTPException(status_code=400, detail="Missing repo_url for retry")
        ref = data.get("ref") or data.get("branch")
        container_port = data.get("container_port")
        retry_env = env or {}
        retry_build_args = build_args or {}
        env_keys = sorted(retry_env.keys()) if retry_env else sorted(data.get("env_keys") or [])
        build_arg_keys = (
            sorted(retry_build_args.keys()) if retry_build_args else sorted(data.get("build_arg_keys") or [])
        )
        attempt = int(data.get("attempt") or 1) + 1
        retry_of = data.get("retry_of") or case_id
        dockerfile_path = data.get("dockerfile_path")
        compose_file = data.get("compose_file")
        context_path = data.get("context_path")
        docker_build_network = data.get("docker_build_network")
        docker_no_cache = data.get("docker_no_cache")
        docker_buildkit = data.get("docker_buildkit")
        git_submodules = data.get("git_submodules")
        git_lfs = data.get("git_lfs")
        mode = data.get("run_mode") or data.get("mode")
        auto_mode = bool(data.get("auto_mode"))
        auto_manual = bool(data.get("auto_manual")) if data.get("auto_manual") is not None else True
        update_case(
            case_id,
            {
                "status": "PENDING",
                "stage": "system",
                "error_code": None,
                "error_message": None,
                "env_keys": env_keys,
                "build_arg_keys": build_arg_keys,
                "attempt": attempt,
                "retry_of": retry_of,
            },
        )
        append_system_log(case_id, "Retry requested")
        build_and_run.delay(
            case_id,
            repo_url,
            ref,
            container_port,
            retry_env,
            False,
            dockerfile_path,
            compose_file,
            context_path,
            docker_build_network,
            docker_no_cache,
            retry_build_args,
            git_submodules,
            git_lfs,
            mode,
            auto_mode,
            auto_manual,
            docker_buildkit,
        )
        return CaseActionResponse(case_id=case_id, action=action_key, status="PENDING", message="Retry started")

    if action_key == "archive":
        update_case(case_id, {"archived": True, "archived_at": time.time()})
        append_system_log(case_id, "Case archived")
        return CaseActionResponse(case_id=case_id, action=action_key, status="ARCHIVED", message="Archived")

    raise HTTPException(status_code=400, detail="Unsupported action")


@app.post("/cases/{case_id}/actions", response_model=CaseActionResponse)
async def case_actions(case_id: str, payload: CaseActionRequest, request: Request) -> CaseActionResponse:
    identity = _request_identity_for_cases(request)
    return handle_case_action(case_id, payload.action, identity, payload.env)


@app.post("/cases/{case_id}/stop", response_model=CaseActionResponse)
async def case_stop(case_id: str, request: Request) -> CaseActionResponse:
    identity = _request_identity_for_cases(request)
    return handle_case_action(case_id, "stop", identity)


@app.post("/cases/{case_id}/restart", response_model=CaseActionResponse)
async def case_restart(case_id: str, request: Request) -> CaseActionResponse:
    identity = _request_identity_for_cases(request)
    return handle_case_action(case_id, "restart", identity)


@app.post("/cases/{case_id}/retry", response_model=CaseActionResponse)
async def case_retry(
    case_id: str,
    request: Request,
    payload: CaseRetryRequest = Body(default_factory=CaseRetryRequest),
) -> CaseActionResponse:
    identity = _request_identity_for_cases(request)
    env = payload.env
    build_args = payload.docker_build_args
    return handle_case_action(case_id, "retry", identity, env, build_args)


@app.post("/cases/{case_id}/archive", response_model=CaseActionResponse)
async def case_archive(case_id: str, request: Request) -> CaseActionResponse:
    identity = _request_identity_for_cases(request)
    return handle_case_action(case_id, "archive", identity)


@app.post("/cases/{case_id}/analyze")
async def trigger_analyze(
    case_id: str,
    request: Request,
    payload: AnalyzeRequest = Body(default_factory=AnalyzeRequest),
) -> Dict[str, Any]:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    repo_url = data.get("repo_url")
    commit_sha = data.get("commit_sha") or "unknown"
    if repo_url:
        cache_key = repo_cache_key(repo_url, commit_sha)
        locked = acquire_analyze_lock(cache_key, ANALYZE_LOCK_TTL_SECONDS)
        if not locked:
            return {
                "case_id": case_id,
                "analyze_status": "RUNNING",
                "report_ready": bool(data.get("report_ready", False)),
                "message": "already running",
            }
    update_case(
        case_id,
        {
            "analyze_status": "PENDING",
            "report_ready": False if payload.force else bool(data.get("report_ready", False)),
            "report_cached": False,
            "analyze_error_code": None,
            "analyze_error_message": None,
        },
    )
    analyze_case.delay(case_id, payload.force, payload.mode)
    return {
        "case_id": case_id,
        "analyze_status": "PENDING",
        "report_ready": False if payload.force else bool(data.get("report_ready", False)),
    }


@app.get("/cases/{case_id}/report", response_model=ReportResponse)
async def get_report(case_id: str, request: Request) -> ReportResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    repo_url = data.get("repo_url")
    commit_sha = data.get("commit_sha")
    if not repo_url or not commit_sha:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "REPORT_NOT_READY",
                "message": "Report not ready",
            },
        )
    store = ReportStore()
    report = store.load_report(repo_url, commit_sha)
    if not report:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "REPORT_NOT_READY",
                "message": "Report not ready",
            },
        )
    business_summary = report.get("business_summary")
    if not isinstance(business_summary, dict):
        business_summary = None
    return ReportResponse(
        markdown=str(report.get("markdown") or ""),
        mermaids=list(report.get("mermaids") or []),
        assets=list(report.get("assets") or []),
        validation=report.get("validation") or {},
        commit_sha=str(report.get("commit_sha") or commit_sha),
        created_at=float(report.get("created_at") or 0),
        business_summary=business_summary,
    )


@app.post("/cases/{case_id}/visualize")
async def trigger_visualize(
    case_id: str,
    request: Request,
    payload: VisualizeRequest = Body(default_factory=VisualizeRequest),
) -> Dict[str, Any]:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    repo_url = data.get("repo_url")
    commit_sha = data.get("commit_sha") or "unknown"
    if repo_url:
        cache_key = visual_cache_key(repo_url, commit_sha)
        locked = acquire_visualize_lock(cache_key, VISUAL_LOCK_TTL_SECONDS)
        if not locked:
            return {
                "case_id": case_id,
                "visual_status": "RUNNING",
                "visual_ready": bool(data.get("visual_ready", False)),
                "message": "already running",
            }
    update_case(
        case_id,
        {
            "visual_status": "PENDING",
            "visual_ready": False,
            "visual_cached": False,
            "visual_error_code": None,
            "visual_error_message": None,
        },
    )
    requested_kinds = payload.kinds
    if not VISUAL_VIDEO_ENABLED and requested_kinds:
        filtered = [kind for kind in requested_kinds if str(kind).strip().lower() != "video"]
        requested_kinds = filtered or None
    visualize_case.delay(case_id, payload.force, requested_kinds)
    return {
        "case_id": case_id,
        "visual_status": "PENDING",
        "visual_ready": False,
    }


@app.post("/cases/{case_id}/understand")
async def trigger_understand(
    case_id: str,
    request: Request,
    payload: UnderstandRequest = Body(default_factory=UnderstandRequest),
) -> Dict[str, Any]:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    repo_url = data.get("repo_url")
    commit_sha = data.get("commit_sha") or "unknown"
    if repo_url:
        cache_key = visual_cache_key(repo_url, commit_sha)
        locked = acquire_visualize_lock(cache_key, VISUAL_LOCK_TTL_SECONDS)
        if not locked:
            return {
                "case_id": case_id,
                "visual_status": "RUNNING",
                "visual_ready": bool(data.get("visual_ready", False)),
                "message": "already running",
            }
    update_case(
        case_id,
        {
            "visual_status": "PENDING",
            "visual_ready": False,
            "visual_cached": False,
            "visual_error_code": None,
            "visual_error_message": None,
        },
    )
    visualize_case.delay(case_id, payload.force, None)
    return {
        "case_id": case_id,
        "visual_status": "PENDING",
        "visual_ready": False,
    }


@app.get("/cases/{case_id}/status", response_model=UnderstandStatusResponse)
async def get_understand_status(case_id: str, request: Request) -> UnderstandStatusResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    state, message = _infer_understand_state(case_id, data)
    return UnderstandStatusResponse(
        case_id=case_id,
        repo_url=data.get("repo_url"),
        state=state,
        message=message,
        visual_status=data.get("visual_status"),
        visual_ready=bool(data.get("visual_ready")),
        visual_error_code=data.get("visual_error_code"),
        visual_error_message=data.get("visual_error_message"),
        updated_at=data.get("updated_at"),
    )


@app.get("/cases/{case_id}/result", response_model=UnderstandResultResponse)
async def get_understand_result(case_id: str, request: Request) -> UnderstandResultResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    repo_url = data.get("repo_url")
    commit_sha = data.get("commit_sha")
    state, message = _infer_understand_state(case_id, data)

    assets: List[VisualAssetResponse] = []
    status = "UNKNOWN"
    created_at = 0.0
    cached = None
    if repo_url and commit_sha:
        store = VisualStore()
        visuals = store.load_visuals(repo_url, commit_sha)
        if visuals:
            response = build_visuals_response(case_id, commit_sha, visuals)
            assets = response.assets
            status = response.status
            created_at = response.created_at
            cached = response.cached
    if not assets and data.get("visual_status") == "FAILED":
        status = "FAILED"
        message = "讲解失败，但你仍可查看已生成的内容。"

    return UnderstandResultResponse(
        case_id=case_id,
        repo_url=repo_url,
        state=state,
        status=status,
        message=message,
        assets=assets,
        created_at=created_at,
        cached=cached,
    )

@app.post("/cases/{case_id}/visualize/video")
async def trigger_visualize_video(
    case_id: str,
    request: Request,
    payload: VisualizeVideoRequest = Body(default_factory=VisualizeVideoRequest),
) -> Dict[str, Any]:
    if not VISUAL_VIDEO_ENABLED:
        raise HTTPException(
            status_code=410,
            detail="video rendering is disabled; use /cases/{case_id}/visualize for image/text walkthrough",
        )
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    repo_url = data.get("repo_url")
    commit_sha = data.get("commit_sha") or "unknown"
    if repo_url:
        cache_key = visual_cache_key(repo_url, commit_sha)
        locked = acquire_visualize_lock(cache_key, VISUAL_LOCK_TTL_SECONDS)
        if not locked:
            return {
                "case_id": case_id,
                "visual_status": "RUNNING",
                "visual_ready": bool(data.get("visual_ready", False)),
                "message": "already running",
            }
    update_case(
        case_id,
        {
            "visual_status": "PENDING",
            "visual_ready": False,
            "visual_cached": False,
            "visual_error_code": None,
            "visual_error_message": None,
        },
    )
    visualize_case.delay(case_id, payload.force, ["video"])
    return {
        "case_id": case_id,
        "visual_status": "PENDING",
        "visual_ready": False,
    }


@app.get("/cases/{case_id}/visuals", response_model=VisualsResponse)
async def get_visuals(case_id: str, request: Request) -> VisualsResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    repo_url = data.get("repo_url")
    commit_sha = data.get("commit_sha")
    if not repo_url or not commit_sha:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "VISUAL_NOT_READY",
                "message": "Visual assets not ready",
            },
        )
    store = VisualStore()
    visuals = store.load_visuals(repo_url, commit_sha)
    if not visuals:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "VISUAL_NOT_READY",
                "message": "Visual assets not ready",
            },
        )
    return build_visuals_response(case_id, commit_sha, visuals)


@app.get("/cases/{case_id}/visuals/{filename}")
async def get_visual_file(case_id: str, filename: str, request: Request) -> FileResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid file name")
    repo_url = data.get("repo_url")
    commit_sha = data.get("commit_sha")
    if not repo_url or not commit_sha:
        raise HTTPException(status_code=404, detail="Visual assets not ready")
    target_dir = visuals_dir(repo_url, commit_sha)
    target_path = target_dir / filename
    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Visual file not found")
    return FileResponse(path=str(target_path))


@app.post("/cases/{case_id}/manual", response_model=ManualStatusResponse)
async def trigger_manual(case_id: str, request: Request) -> ManualStatusResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    update_case(
        case_id,
        {
            "manual_status": "PENDING",
            "manual_error_code": None,
            "manual_error_message": None,
        },
    )
    set_manual_status(case_id, "PENDING")
    generate_manual_task.delay(case_id)
    return ManualStatusResponse(case_id=case_id, status="PENDING")


@app.get("/cases/{case_id}/manual/status", response_model=ManualStatusResponse)
async def manual_status(case_id: str, request: Request) -> ManualStatusResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    data = _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    status_data = get_manual_status(case_id) or {}
    status = status_data.get("status") or data.get("manual_status") or "PENDING"
    status = str(status).upper()
    if status not in {"PENDING", "RUNNING", "SUCCESS", "FAILED"}:
        status = "PENDING"
    return ManualStatusResponse(
        case_id=case_id,
        status=status,
        generated_at=status_data.get("generated_at") or data.get("manual_generated_at"),
        error_code=status_data.get("error_code") or data.get("manual_error_code"),
        error_message=status_data.get("error_message") or data.get("manual_error_message"),
    )


@app.get("/cases/{case_id}/manual", response_model=ManualResponse)
async def get_manual_content(case_id: str, request: Request) -> ManualResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    markdown, meta = get_manual(case_id)
    if not markdown or not meta:
        raise HTTPException(status_code=404, detail="Manual not found")
    return ManualResponse(case_id=case_id, manual_markdown=markdown, meta=ManualMeta(**meta))


@app.websocket("/cases/{case_id}/logs")
@app.websocket("/ws/logs/{case_id}")
async def case_logs(websocket: WebSocket, case_id: str) -> None:
    try:
        identity = await authenticate_websocket(websocket)
    except AuthError:
        await websocket.close(code=4401)
        return
    data = get_case(case_id) or {}
    if not data:
        await websocket.close(code=4404)
        return
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    if not _is_case_visible_to_identity(identity, data, actor_tenant_id=actor_tenant_id):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    if REDIS_DISABLED:
        for entry in get_logs(case_id):
            await websocket.send_json(entry)
        await websocket.close()
        return
    redis_client = get_async_redis_client()
    pubsub = redis_client.pubsub()
    channel = log_channel(case_id)
    try:
        for entry in get_logs(case_id):
            await websocket.send_json(entry)
        await pubsub.subscribe(channel)
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not message or message.get("type") != "message":
                continue
            payload = decode_log_entry(message["data"])
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        await redis_client.close()


@app.get("/cases/{case_id}/logs")
async def http_logs(
    case_id: str,
    request: Request,
    limit: int = Query(200, ge=1, le=2000, description="Number of lines from tail"),
    offset: int = Query(0, ge=0, description="Offset from tail"),
    format: Optional[str] = Query(None, description="json | jsonl"),
) -> Any:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    entries = get_logs_slice(case_id, offset=offset, limit=limit)
    if format == "jsonl":
        return PlainTextResponse(build_jsonl(entries), media_type="application/jsonl")
    return entries


@app.get("/cases/{case_id}/logs/download")
async def download_logs(
    case_id: str,
    request: Request,
    limit: int = Query(1000, ge=1, le=2000, description="Number of lines from tail"),
    offset: int = Query(0, ge=0, description="Offset from tail"),
) -> StreamingResponse:
    identity = _request_identity_for_cases(request)
    actor_tenant_id = _resolve_identity_tenant_for_cases(identity)
    _get_case_for_identity(case_id, identity, actor_tenant_id=actor_tenant_id)
    entries = get_logs_slice(case_id, offset=offset, limit=limit)
    payload = build_jsonl(entries).encode("utf-8")
    headers = {"Content-Disposition": f"attachment; filename={case_id}.jsonl"}
    return StreamingResponse(iter([payload]), media_type="application/jsonl", headers=headers)
