from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

import main
from billing.db import build_session_factory
from billing.db import init_billing_db as billing_init_billing_db
from billing.db import session_scope as billing_session_scope


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    data = response.json()
    return str(data["access_token"])


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_billing_request_validation_rejects_injection_like_inputs(monkeypatch, tmp_path: Path) -> None:
    """
    Basic sanity checks for request-model validation.

    This is not a full security test, but it ensures that obvious injection-like
    characters in identifiers are rejected early (422) and do not reach DB logic.
    """

    db_path = tmp_path / "billing_validation.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    monkeypatch.setattr(main, "APP_ENV", "dev")
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(
        main,
        "AUTH_USERS_JSON",
        json.dumps(
            {
                "admin": {"password": "admin123", "role": "admin"},
                "alice": {"password": "alice123", "role": "user"},
            }
        ),
    )
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 7200)
    monkeypatch.setattr(main, "session_scope", _session_scope_override)
    monkeypatch.setattr(main, "init_billing_db", lambda: billing_init_billing_db(engine))

    with TestClient(main.app) as client:
        admin_token = _login(client, "admin", "admin123")
        user_token = _login(client, "alice", "alice123")

        # Admin plan create: plan code pattern should reject semicolons/quotes/spaces.
        bad_plan = client.post(
            "/admin/billing/plans",
            headers=_auth_header(admin_token),
            json={
                "code": 'pro;DROP TABLE billing_orders;--',
                "name": "Pro",
                "price_cents": 9900,
                "monthly_points": 1000,
                "currency": "usd",
            },
        )
        assert bad_plan.status_code == 422

        # Create a valid plan so /billing/checkout has a real target.
        ok_plan = client.post(
            "/admin/billing/plans",
            headers=_auth_header(admin_token),
            json={
                "code": "pro_valid",
                "name": "Pro",
                "price_cents": 9900,
                "monthly_points": 1000,
                "currency": "usd",
            },
        )
        assert ok_plan.status_code == 200, ok_plan.text

        # Checkout: plan_code pattern should reject obvious injection-like characters.
        bad_checkout = client.post(
            "/billing/checkout",
            headers=_auth_header(user_token),
            json={"plan_code": "pro_valid;--", "idempotency_key": "k1"},
        )
        assert bad_checkout.status_code == 422

        # Checkout: idempotency_key pattern should reject semicolons.
        bad_idem = client.post(
            "/billing/checkout",
            headers=_auth_header(user_token),
            json={"plan_code": "pro_valid", "idempotency_key": "k1;rm -rf /"},
        )
        assert bad_idem.status_code == 422

        # Dev simulate: external_order_id pattern should reject semicolons.
        bad_simulate = client.post(
            "/billing/dev/simulate-payment",
            headers=_auth_header(user_token),
            json={"external_order_id": "ord_ext_123;--"},
        )
        assert bad_simulate.status_code == 422

        # Recommendations: reject obvious XSS-like payloads before service execution.
        bad_recommend = client.post(
            "/recommendations",
            headers=_auth_header(user_token),
            data={"query": "<script>alert(1)</script>", "mode": "quick", "limit": "8"},
        )
        assert bad_recommend.status_code == 422

    engine.dispose()
