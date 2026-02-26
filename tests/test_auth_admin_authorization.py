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
    token = str(response.json().get("access_token") or "")
    assert token
    return token


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _apply_auth_overrides(monkeypatch, session_factory, engine) -> None:
    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    monkeypatch.setattr(main, "APP_ENV", "dev")
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 3600)
    monkeypatch.setattr(
        main,
        "AUTH_USERS_JSON",
        json.dumps(
            {
                "root": {"password": "root123", "role": "root"},
                "admin": {"password": "admin123", "role": "admin"},
                "alice": {"password": "alice123", "role": "user"},
            }
        ),
    )
    monkeypatch.setattr(main, "FEATURE_SAAS_ADMIN_API", True)
    monkeypatch.setattr(main, "session_scope", _session_scope_override)
    monkeypatch.setattr(main, "init_billing_db", lambda: billing_init_billing_db(engine))
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)


def test_admin_and_root_authorization_guards(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "auth_admin_guard.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_auth_overrides(monkeypatch, session_factory, engine)

    with TestClient(main.app) as client:
        user_token = _login(client, "alice", "alice123")
        admin_token = _login(client, "admin", "admin123")
        root_token = _login(client, "root", "root123")

        # normal user denied admin routes
        user_denied_admin = client.get("/admin/billing/orders", headers=_auth_header(user_token))
        assert user_denied_admin.status_code == 403

        user_denied_saas_admin = client.get("/admin/saas/plans", headers=_auth_header(user_token))
        assert user_denied_saas_admin.status_code == 403

        # admin allowed admin routes
        admin_orders = client.get("/admin/billing/orders", headers=_auth_header(admin_token))
        assert admin_orders.status_code == 200, admin_orders.text

        admin_saas_plans = client.get("/admin/saas/plans", headers=_auth_header(admin_token))
        assert admin_saas_plans.status_code == 200, admin_saas_plans.text

        # non-root admin denied root-only routes
        admin_create_tenant = client.post(
            "/admin/tenants",
            headers=_auth_header(admin_token),
            json={"name": "Tenant A", "code": "tenant-a", "active": True},
        )
        assert admin_create_tenant.status_code == 403

        # root allowed root-only routes
        root_create_tenant = client.post(
            "/admin/tenants",
            headers=_auth_header(root_token),
            json={"name": "Tenant A", "code": "tenant-a", "active": True},
        )
        assert root_create_tenant.status_code == 200, root_create_tenant.text
        tenant_id = str(root_create_tenant.json()["tenant_id"])

        root_update_tenant = client.put(
            f"/admin/tenants/{tenant_id}",
            headers=_auth_header(root_token),
            json={"name": "Tenant A Updated"},
        )
        assert root_update_tenant.status_code == 200, root_update_tenant.text

        admin_update_tenant = client.put(
            f"/admin/tenants/{tenant_id}",
            headers=_auth_header(admin_token),
            json={"name": "Should be denied"},
        )
        assert admin_update_tenant.status_code == 403

    engine.dispose()
