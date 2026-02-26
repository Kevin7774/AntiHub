from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

import main
from auth import hash_password_bcrypt
from billing.db import build_session_factory
from billing.db import init_billing_db as billing_init_billing_db
from billing.db import session_scope as billing_session_scope
from tenant_context import TENANT_CONTEXT_HEADER


def _auth_header(token: str, tenant_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if tenant_id:
        headers[TENANT_CONTEXT_HEADER] = tenant_id
    return headers


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
    monkeypatch.setattr(main, "STARTUP_BOOTSTRAP_ENABLED", False)


def test_tenant_context_flag_off_ignores_header(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_ctx_flag_off.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant_a = repo.create_tenant(code="team-a", name="Team A", active=True)
        tenant_b = repo.create_tenant(code="team-b", name="Team B", active=True)
        repo.upsert_auth_user(
            username="alice",
            password_hash=hash_password_bcrypt("alice12345"),
            role="user",
            active=True,
            tenant_id=str(tenant_a.id),
        )
        tenant_b_id = str(tenant_b.id)
        tenant_a_id = str(tenant_a.id)

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", False)

    with TestClient(main.app) as client:
        token = _login(client, "alice", "alice12345")
        response = client.get(
            "/auth/permissions/me",
            headers=_auth_header(token, tenant_b_id),
        )
        assert response.status_code == 200, response.text
        assert response.json().get("tenant_id") == tenant_a_id

    engine.dispose()


def test_tenant_context_flag_on_denies_cross_tenant_without_membership(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_ctx_denied.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant_a = repo.create_tenant(code="tenant-a", name="Tenant A", active=True)
        tenant_b = repo.create_tenant(code="tenant-b", name="Tenant B", active=True)
        repo.upsert_auth_user(
            username="bob",
            password_hash=hash_password_bcrypt("bob12345"),
            role="user",
            active=True,
            tenant_id=str(tenant_a.id),
        )
        tenant_b_id = str(tenant_b.id)

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        token = _login(client, "bob", "bob12345")
        denied = client.get(
            "/auth/permissions/me",
            headers=_auth_header(token, tenant_b_id),
        )
        assert denied.status_code == 403
        assert "cross-tenant access denied" in str(denied.json().get("detail") or "")

    engine.dispose()


def test_tenant_context_flag_on_allows_membership_tenant_switch(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_ctx_member_allowed.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant_a = repo.create_tenant(code="workspace-a", name="Workspace A", active=True)
        tenant_b = repo.create_tenant(code="workspace-b", name="Workspace B", active=True)
        tenant_a_id = str(tenant_a.id)
        tenant_b_id = str(tenant_b.id)

        repo.upsert_auth_user(
            username="carol",
            password_hash=hash_password_bcrypt("carol12345"),
            role="user",
            active=True,
            tenant_id=tenant_a_id,
        )
        repo.upsert_tenant_member(
            tenant_id=tenant_b_id,
            username="carol",
            role="member",
            active=True,
            is_default=False,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        token = _login(client, "carol", "carol12345")
        switched = client.get(
            "/auth/permissions/me",
            headers=_auth_header(token, tenant_b_id),
        )
        assert switched.status_code == 200, switched.text
        assert switched.json().get("tenant_id") == tenant_b_id

        legacy = client.get("/auth/permissions/me", headers=_auth_header(token))
        assert legacy.status_code == 200, legacy.text
        assert legacy.json().get("tenant_id") == tenant_a_id

    engine.dispose()


def test_tenant_context_flag_on_root_can_switch_without_membership(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_ctx_root_allowed.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="ops-team", name="Ops Team", active=True)
        tenant_id = str(tenant.id)
        repo.upsert_auth_user(
            username="root",
            password_hash=hash_password_bcrypt("root12345"),
            role="root",
            active=True,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        token = _login(client, "root", "root12345")
        switched = client.get(
            "/auth/permissions/me",
            headers=_auth_header(token, tenant_id),
        )
        assert switched.status_code == 200, switched.text
        assert switched.json().get("tenant_id") == tenant_id

    engine.dispose()
