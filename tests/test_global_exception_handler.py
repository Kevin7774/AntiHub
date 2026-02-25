from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

import main
from billing import BillingRateLimiter


def test_unhandled_exceptions_are_normalized(monkeypatch) -> None:
    @contextmanager
    def _noop_session_scope():
        yield None

    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(
        main,
        "AUTH_USERS_JSON",
        json.dumps({"alice": {"password": "alice123", "role": "user"}}),
    )
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 3600)
    monkeypatch.setattr(main, "session_scope", _noop_session_scope)
    monkeypatch.setattr(main, "init_billing_db", lambda: None)
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)
    monkeypatch.setattr(main, "resolve_user_rpm", lambda _username: 100)

    limiter = BillingRateLimiter()
    limiter._client = None
    monkeypatch.setattr(main, "BILLING_RATE_LIMITER", limiter)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom: should not leak")

    monkeypatch.setattr(main.BillingRepository, "list_plans", _boom)

    with TestClient(main.app, raise_server_exceptions=False) as client:
        login = client.post("/auth/login", json={"username": "alice", "password": "alice123"})
        assert login.status_code == 200, login.text
        token = str(login.json()["access_token"])
        response = client.get("/billing/plans", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 500, response.text
        payload = response.json()
        assert payload.get("error_code") == "INTERNAL_SERVER_ERROR"
        assert payload.get("message") == "internal server error"
        trace_id = str(payload.get("trace_id") or "")
        assert trace_id
        assert response.headers.get("X-Trace-Id") == trace_id
        assert "boom" not in response.text.lower()


def test_security_headers_present_on_api_response(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "")
    monkeypatch.setattr(main, "init_billing_db", lambda: None)
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)
    with TestClient(main.app) as client:
        response = client.get("/health")
        assert response.status_code == 200, response.text
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert response.headers.get("Content-Security-Policy")


def test_health_report_includes_runtime_checks(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "")
    monkeypatch.setattr(main, "init_billing_db", lambda: None)
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)
    with TestClient(main.app) as client:
        response = client.get("/health")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert isinstance(payload.get("details"), dict)
        assert "redis" in payload
        assert "db" in payload
        assert "docker" in payload
        assert "openclaw" in payload
        assert "disk" in payload
