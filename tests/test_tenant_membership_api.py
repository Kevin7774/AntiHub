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
    monkeypatch.setattr(main, "STARTUP_BOOTSTRAP_ENABLED", False)


def test_root_can_manage_tenant_members_and_settings(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_root_ok.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="team-a", name="Team A", active=True)
        tenant_id = str(tenant.id)
        repo.upsert_auth_user(
            username="root",
            password_hash=hash_password_bcrypt("root12345"),
            role="root",
            active=True,
        )
        repo.upsert_auth_user(
            username="alice",
            password_hash=hash_password_bcrypt("alice12345"),
            role="user",
            active=True,
            tenant_id=tenant_id,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        token = _login(client, "root", "root12345")

        upsert_member = client.put(
            f"/admin/tenants/{tenant_id}/members/alice",
            headers=_auth_header(token),
            json={"role": "member", "active": True, "is_default": False, "metadata": {"source": "manual"}},
        )
        assert upsert_member.status_code == 200, upsert_member.text
        assert upsert_member.json().get("username") == "alice"

        listed_members = client.get(
            f"/admin/tenants/{tenant_id}/members",
            headers=_auth_header(token),
        )
        assert listed_members.status_code == 200, listed_members.text
        assert any(item.get("username") == "alice" for item in listed_members.json())

        upsert_setting = client.put(
            f"/admin/tenants/{tenant_id}/settings/feature.deep_search",
            headers=_auth_header(token),
            json={"value": {"enabled": True, "rpm": 120}, "metadata": {"source": "ops"}},
        )
        assert upsert_setting.status_code == 200, upsert_setting.text
        assert upsert_setting.json().get("key") == "feature.deep_search"

        get_setting = client.get(
            f"/admin/tenants/{tenant_id}/settings/feature.deep_search",
            headers=_auth_header(token),
        )
        assert get_setting.status_code == 200, get_setting.text
        assert get_setting.json().get("value", {}).get("enabled") is True

        deactivated = client.delete(
            f"/admin/tenants/{tenant_id}/members/alice",
            headers=_auth_header(token),
        )
        assert deactivated.status_code == 200, deactivated.text
        assert deactivated.json().get("active") is False

        listed_with_inactive = client.get(
            f"/admin/tenants/{tenant_id}/members",
            headers=_auth_header(token),
            params={"include_inactive": True},
        )
        assert listed_with_inactive.status_code == 200, listed_with_inactive.text
        assert any(item.get("username") == "alice" and item.get("active") is False for item in listed_with_inactive.json())

    engine.dispose()


def test_non_admin_user_denied_membership_management(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_user_denied.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="team-b", name="Team B", active=True)
        tenant_id = str(tenant.id)
        repo.upsert_auth_user(
            username="bob",
            password_hash=hash_password_bcrypt("bob12345"),
            role="user",
            active=True,
            tenant_id=tenant_id,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        token = _login(client, "bob", "bob12345")
        denied = client.get(
            f"/admin/tenants/{tenant_id}/members",
            headers=_auth_header(token),
        )
        assert denied.status_code == 403

    engine.dispose()


def test_tenant_admin_denied_membership_management_in_root_only_mvp(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_admin_denied.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="team-c", name="Team C", active=True)
        tenant_id = str(tenant.id)
        repo.upsert_auth_user(
            username="admin_c",
            password_hash=hash_password_bcrypt("admin12345"),
            role="admin",
            active=True,
            tenant_id=tenant_id,
        )
        repo.upsert_auth_user(
            username="member_c",
            password_hash=hash_password_bcrypt("member12345"),
            role="user",
            active=True,
            tenant_id=tenant_id,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        token = _login(client, "admin_c", "admin12345")
        denied = client.put(
            f"/admin/tenants/{tenant_id}/members/member_c",
            headers=_auth_header(token),
            json={"role": "member", "active": True, "is_default": False},
        )
        assert denied.status_code == 403

    engine.dispose()


def test_membership_api_flag_off_returns_feature_disabled(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_flag_off.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="team-d", name="Team D", active=True)
        tenant_id = str(tenant.id)
        repo.upsert_auth_user(
            username="root",
            password_hash=hash_password_bcrypt("root12345"),
            role="root",
            active=True,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", False)

    with TestClient(main.app) as client:
        token = _login(client, "root", "root12345")
        disabled = client.get(
            f"/admin/tenants/{tenant_id}/members",
            headers=_auth_header(token),
        )
        assert disabled.status_code == 404
        assert "feature disabled" in str(disabled.json().get("detail") or "")

        # Existing admin IAM routes remain available when tenant foundation flag is OFF.
        users = client.get("/admin/users", headers=_auth_header(token))
        assert users.status_code == 200, users.text

    engine.dispose()
