from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

import main
from auth import hash_password_bcrypt
from billing.db import build_session_factory
from billing.db import init_billing_db as billing_init_billing_db
from billing.db import session_scope as billing_session_scope


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = str(response.json().get("access_token") or "")
    assert token
    return token


def _apply_auth_test_overrides(monkeypatch, session_factory, engine) -> None:
    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(main, "AUTH_USERS_JSON", "")
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 3600)
    monkeypatch.setattr(main, "session_scope", _session_scope_override)
    monkeypatch.setattr(main, "init_billing_db", lambda: billing_init_billing_db(engine))
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)


def test_register_creates_tenant_and_returns_tenant_profile(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "auth_register_tenant.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_auth_test_overrides(monkeypatch, session_factory, engine)

    with TestClient(main.app) as client:
        registered = client.post(
            "/auth/register",
            json={"username": "alice", "password": "alice12345", "tenant_name": "Alice Studio"},
        )
        assert registered.status_code == 200, registered.text
        data = registered.json()
        assert data.get("user", {}).get("username") == "alice"
        assert data.get("user", {}).get("tenant_name") == "Alice Studio"
        assert data.get("user", {}).get("tenant_code")
        token = str(data.get("access_token") or "")
        assert token

        me = client.get("/auth/me", headers=_auth_header(token))
        assert me.status_code == 200, me.text
        profile = me.json()
        assert profile.get("username") == "alice"
        assert profile.get("tenant_name") == "Alice Studio"
        assert profile.get("tenant_id")

        forbidden = client.get("/admin/tenants", headers=_auth_header(token))
        assert forbidden.status_code == 403

    engine.dispose()


def test_register_rejects_duplicate_username(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "auth_register_duplicate.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_auth_test_overrides(monkeypatch, session_factory, engine)

    with TestClient(main.app) as client:
        first = client.post(
            "/auth/register",
            json={"username": "same_user", "password": "same_user_123", "tenant_name": "Tenant A"},
        )
        assert first.status_code == 200, first.text

        second = client.post(
            "/auth/register",
            json={"username": "same_user", "password": "same_user_456", "tenant_name": "Tenant B"},
        )
        assert second.status_code == 409, second.text

    engine.dispose()


def test_root_can_create_and_list_tenants(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "admin_tenants.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        repo.upsert_auth_user(
            username="root",
            password_hash=hash_password_bcrypt("root12345"),
            role="root",
            active=True,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)

    with TestClient(main.app) as client:
        root_token = _login(client, "root", "root12345")

        created = client.post(
            "/admin/tenants",
            headers=_auth_header(root_token),
            json={"name": "Ops Team", "code": "ops-team", "active": True},
        )
        assert created.status_code == 200, created.text
        tenant = created.json()
        assert tenant.get("code") == "ops-team"
        assert tenant.get("name") == "Ops Team"

        listed = client.get("/admin/tenants", headers=_auth_header(root_token))
        assert listed.status_code == 200, listed.text
        rows = listed.json()
        assert isinstance(rows, list)
        assert any(item.get("code") == "ops-team" for item in rows)

    engine.dispose()
