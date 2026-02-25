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

    monkeypatch.setattr(main, "APP_ENV", "dev")
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(main, "AUTH_USERS_JSON", "")
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 3600)
    monkeypatch.setattr(main, "session_scope", _session_scope_override)
    monkeypatch.setattr(main, "init_billing_db", lambda: billing_init_billing_db(engine))
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)


def test_root_bootstrap_and_permission_snapshot(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "iam_root_bootstrap.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "STARTUP_BOOTSTRAP_ENABLED", True)
    monkeypatch.setattr(main, "ROOT_ADMIN_USERNAME", "root")
    monkeypatch.setattr(main, "ROOT_ADMIN_PASSWORD", "root12345")
    monkeypatch.setattr(main, "ROOT_ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(main, "ROOT_ADMIN_FORCE_SYNC", False)

    with TestClient(main.app) as client:
        root_token = _login(client, "root", "root12345")
        permissions = client.get("/auth/permissions/me", headers=_auth_header(root_token))
        assert permissions.status_code == 200, permissions.text
        payload = permissions.json()
        assert payload.get("role") == "root"
        scopes = payload.get("scopes") or []
        assert "iam:root" in scopes
        assert "tenant:write_global" in scopes

    engine.dispose()


def test_admin_is_tenant_scoped_for_user_management(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "iam_admin_scope.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="team-a", name="Team A", active=True)
        tenant_id = str(tenant.id)
        repo.upsert_auth_user(
            username="admin_a",
            password_hash=hash_password_bcrypt("admin12345"),
            role="admin",
            active=True,
            tenant_id=tenant_id,
        )
        other_tenant = repo.create_tenant(code="team-b", name="Team B", active=True)
        other_tenant_id = str(other_tenant.id)
        repo.upsert_auth_user(
            username="member_b",
            password_hash=hash_password_bcrypt("member12345"),
            role="user",
            active=True,
            tenant_id=other_tenant_id,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "STARTUP_BOOTSTRAP_ENABLED", False)

    with TestClient(main.app) as client:
        admin_token = _login(client, "admin_a", "admin12345")
        # ABAC: admin cannot query other tenant users
        forbidden = client.get(
            "/admin/users",
            params={"tenant_id": other_tenant_id},
            headers=_auth_header(admin_token),
        )
        assert forbidden.status_code == 403

        # RBAC: admin cannot create admin role users
        denied_create_admin = client.post(
            "/admin/users",
            headers=_auth_header(admin_token),
            json={
                "username": "admin_like",
                "password": "admin_like_123",
                "role": "admin",
            },
        )
        assert denied_create_admin.status_code == 403

        # Allowed: create user inside own tenant (tenant_id can be omitted).
        created_user = client.post(
            "/admin/users",
            headers=_auth_header(admin_token),
            json={
                "username": "member_a",
                "password": "member_a_123",
                "role": "user",
            },
        )
        assert created_user.status_code == 200, created_user.text
        assert created_user.json().get("tenant_id") == tenant_id

    engine.dispose()


def test_root_can_manage_users_across_tenants(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "iam_root_scope.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="global-team", name="Global Team", active=True)
        repo.upsert_auth_user(
            username="root",
            password_hash=hash_password_bcrypt("root12345"),
            role="root",
            active=True,
        )
        tenant_id = str(tenant.id)

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "STARTUP_BOOTSTRAP_ENABLED", False)

    with TestClient(main.app) as client:
        root_token = _login(client, "root", "root12345")

        created_admin = client.post(
            "/org/users",
            headers=_auth_header(root_token),
            json={
                "username": "tenant_admin",
                "password": "tenant_admin_123",
                "role": "admin",
                "tenant_id": tenant_id,
            },
        )
        assert created_admin.status_code == 200, created_admin.text
        assert created_admin.json().get("role") == "admin"
        assert created_admin.json().get("tenant_id") == tenant_id

        promoted = client.patch(
            "/admin/users/tenant_admin",
            headers=_auth_header(root_token),
            json={"role": "root", "tenant_id": ""},
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json().get("role") == "root"
        assert promoted.json().get("tenant_id") in {None, ""}

    engine.dispose()
