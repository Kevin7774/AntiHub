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


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = str(response.json().get("access_token") or "")
    assert token
    return token


def test_tenant_workspace_returns_identity_and_subscription_snapshot(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_workspace_snapshot.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_auth_test_overrides(monkeypatch, session_factory, engine)

    with TestClient(main.app) as client:
        registered = client.post(
            "/auth/register",
            json={"username": "workspace_user", "password": "workspace_user_123", "tenant_name": "Workspace Tenant"},
        )
        assert registered.status_code == 200, registered.text
        token = str(registered.json().get("access_token") or "")
        assert token

        workspace = client.get("/tenant/workspace", headers=_auth_header(token))
        assert workspace.status_code == 200, workspace.text
        payload = workspace.json()
        assert payload.get("user", {}).get("username") == "workspace_user"
        assert payload.get("tenant", {}).get("name") == "Workspace Tenant"
        assert int(payload.get("member_count") or 0) >= 1
        assert payload.get("subscription", {}).get("status") in {"none", "active"}
        assert int(payload.get("points", {}).get("balance") or 0) >= 0

    engine.dispose()


def test_root_can_query_tenant_users(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_workspace_admin.db"
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
        registered = client.post(
            "/auth/register",
            json={"username": "tenant_member", "password": "tenant_member_123", "tenant_name": "Tenant A"},
        )
        assert registered.status_code == 200, registered.text
        user_token = str(registered.json().get("access_token") or "")
        assert user_token

        me = client.get("/auth/me", headers=_auth_header(user_token))
        assert me.status_code == 200, me.text
        tenant_id = str(me.json().get("tenant_id") or "")
        assert tenant_id

        forbidden = client.get(f"/admin/tenants/{tenant_id}/users", headers=_auth_header(user_token))
        assert forbidden.status_code == 403

        root_token = _login(client, "root", "root12345")
        listed = client.get(f"/admin/tenants/{tenant_id}/users", headers=_auth_header(root_token))
        assert listed.status_code == 200, listed.text
        rows = listed.json()
        assert isinstance(rows, list)
        assert any(row.get("username") == "tenant_member" for row in rows)

    engine.dispose()


def test_admin_abac_blocks_cross_tenant_user_query(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_workspace_admin_abac.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        admin_tenant = repo.create_tenant(code="team-admin", name="Team Admin", active=True)
        repo.upsert_auth_user(
            username="admin",
            password_hash=hash_password_bcrypt("admin12345"),
            role="admin",
            active=True,
            tenant_id=str(admin_tenant.id),
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)

    with TestClient(main.app) as client:
        registered = client.post(
            "/auth/register",
            json={"username": "tenant_member", "password": "tenant_member_123", "tenant_name": "Tenant A"},
        )
        assert registered.status_code == 200, registered.text
        user_token = str(registered.json().get("access_token") or "")
        assert user_token

        me = client.get("/auth/me", headers=_auth_header(user_token))
        assert me.status_code == 200, me.text
        tenant_id = str(me.json().get("tenant_id") or "")
        assert tenant_id

        admin_token = _login(client, "admin", "admin12345")
        denied = client.get(f"/admin/tenants/{tenant_id}/users", headers=_auth_header(admin_token))
        assert denied.status_code == 403

    engine.dispose()
