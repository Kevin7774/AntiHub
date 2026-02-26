from __future__ import annotations

import json
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


def _tenant_audit_events(session_factory, *, event_type: str) -> list[dict[str, object]]:
    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        rows = repo.list_audit_logs(limit=200, offset=0, provider="tenant")
        items: list[dict[str, object]] = []
        for row in rows:
            if str(getattr(row, "event_type", "") or "") != event_type:
                continue
            raw_text = str(getattr(row, "raw_payload", "") or "")
            try:
                payload = json.loads(raw_text) if raw_text else {}
            except Exception:  # noqa: BLE001
                payload = {}
            items.append(
                {
                    "event_type": str(getattr(row, "event_type", "") or ""),
                    "provider": str(getattr(row, "provider", "") or ""),
                    "outcome": str(getattr(row, "outcome", "") or ""),
                    "payload": payload,
                }
            )
        return items


def test_root_can_manage_membership_across_tenants(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_root_cross_tenant.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant_a = repo.create_tenant(code="team-a", name="Team A", active=True)
        tenant_b = repo.create_tenant(code="team-b", name="Team B", active=True)
        tenant_a_id = str(tenant_a.id)
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
            tenant_id=str(tenant_a.id),
        )
        repo.upsert_auth_user(
            username="bob",
            password_hash=hash_password_bcrypt("bob12345"),
            role="user",
            active=True,
            tenant_id=str(tenant_b.id),
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        root_token = _login(client, "root", "root12345")

        same_tenant = client.put(
            f"/admin/tenants/{tenant_a_id}/members/alice",
            headers=_auth_header(root_token),
            json={"role": "member", "active": True, "is_default": False},
        )
        assert same_tenant.status_code == 200, same_tenant.text

        cross_tenant_user = client.put(
            f"/admin/tenants/{tenant_a_id}/members/bob",
            headers=_auth_header(root_token),
            json={"role": "admin", "active": True, "is_default": False},
        )
        assert cross_tenant_user.status_code == 200, cross_tenant_user.text

        listed = client.get(
            f"/admin/tenants/{tenant_a_id}/members",
            headers=_auth_header(root_token),
        )
        assert listed.status_code == 200, listed.text
        usernames = {item.get("username") for item in listed.json()}
        assert {"alice", "bob"}.issubset(usernames)

        deactivated = client.delete(
            f"/admin/tenants/{tenant_a_id}/members/alice",
            headers=_auth_header(root_token),
        )
        assert deactivated.status_code == 200, deactivated.text
        assert deactivated.json().get("active") is False

        setting_upsert = client.put(
            f"/admin/tenants/{tenant_a_id}/settings/feature.deep_search",
            headers=_auth_header(root_token),
            json={"value": {"enabled": True, "rpm": 120}, "metadata": {"source": "ops"}},
        )
        assert setting_upsert.status_code == 200, setting_upsert.text

    upsert_events = _tenant_audit_events(session_factory, event_type="tenant.membership.upsert")
    assert any(
        str(item.get("provider") or "") == "tenant"
        and str(item.get("outcome") or "") == "ok"
        and str(((item.get("payload") or {}).get("tenant_id")) or "") == tenant_a_id
        and str((((item.get("payload") or {}).get("actor") or {}).get("username")) or "") == "root"
        and str((((item.get("payload") or {}).get("target") or {}).get("kind")) or "") == "membership"
        and str((((item.get("payload") or {}).get("target") or {}).get("username")) or "") == "alice"
        for item in upsert_events
    )

    deactivate_events = _tenant_audit_events(session_factory, event_type="tenant.membership.deactivate")
    assert any(
        str(item.get("provider") or "") == "tenant"
        and str(item.get("outcome") or "") == "ok"
        and str(((item.get("payload") or {}).get("tenant_id")) or "") == tenant_a_id
        and str((((item.get("payload") or {}).get("target") or {}).get("username")) or "") == "alice"
        for item in deactivate_events
    )

    setting_events = _tenant_audit_events(session_factory, event_type="tenant.setting.upsert")
    assert any(
        str(item.get("provider") or "") == "tenant"
        and str(item.get("outcome") or "") == "ok"
        and str(((item.get("payload") or {}).get("tenant_id")) or "") == tenant_a_id
        and str((((item.get("payload") or {}).get("target") or {}).get("kind")) or "") == "setting"
        and str((((item.get("payload") or {}).get("target") or {}).get("key")) or "") == "feature.deep_search"
        for item in setting_events
    )

    engine.dispose()


def test_tenant_admin_can_manage_same_tenant_members_with_member_role_only(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_admin_same_tenant.db"
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
        admin_token = _login(client, "admin_c", "admin12345")

        listed = client.get(
            f"/admin/tenants/{tenant_id}/members",
            headers=_auth_header(admin_token),
        )
        assert listed.status_code == 200, listed.text

        allowed = client.put(
            f"/admin/tenants/{tenant_id}/members/member_c",
            headers=_auth_header(admin_token),
            json={"role": "member", "active": True, "is_default": False},
        )
        assert allowed.status_code == 200, allowed.text

        denied_role_escalation = client.put(
            f"/admin/tenants/{tenant_id}/members/member_c",
            headers=_auth_header(admin_token),
            json={"role": "admin", "active": True, "is_default": False},
        )
        assert denied_role_escalation.status_code == 403
        assert "member membership role" in str(denied_role_escalation.json().get("detail") or "")

    engine.dispose()


def test_tenant_admin_cannot_manage_root_membership(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_admin_root_denied.db"
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
        repo.upsert_auth_user(
            username="admin_d",
            password_hash=hash_password_bcrypt("admin12345"),
            role="admin",
            active=True,
            tenant_id=tenant_id,
        )
        repo.upsert_tenant_member(
            tenant_id=tenant_id,
            username="root",
            role="member",
            active=True,
            is_default=False,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        admin_token = _login(client, "admin_d", "admin12345")

        denied_upsert = client.put(
            f"/admin/tenants/{tenant_id}/members/root",
            headers=_auth_header(admin_token),
            json={"role": "member", "active": True, "is_default": False},
        )
        assert denied_upsert.status_code == 403
        assert "root user membership" in str(denied_upsert.json().get("detail") or "")

        denied_delete = client.delete(
            f"/admin/tenants/{tenant_id}/members/root",
            headers=_auth_header(admin_token),
        )
        assert denied_delete.status_code == 403
        assert "root user membership" in str(denied_delete.json().get("detail") or "")

    engine.dispose()


def test_tenant_admin_cannot_manage_outside_same_tenant_even_with_header(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_admin_cross_tenant_denied.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant_a = repo.create_tenant(code="team-e", name="Team E", active=True)
        tenant_b = repo.create_tenant(code="team-f", name="Team F", active=True)
        tenant_b_id = str(tenant_b.id)
        repo.upsert_auth_user(
            username="admin_e",
            password_hash=hash_password_bcrypt("admin12345"),
            role="admin",
            active=True,
            tenant_id=str(tenant_a.id),
        )
        repo.upsert_auth_user(
            username="member_f",
            password_hash=hash_password_bcrypt("member12345"),
            role="user",
            active=True,
            tenant_id=tenant_b_id,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        admin_token = _login(client, "admin_e", "admin12345")

        denied = client.put(
            f"/admin/tenants/{tenant_b_id}/members/member_f",
            headers=_auth_header(admin_token),
            json={"role": "member", "active": True, "is_default": False},
        )
        assert denied.status_code == 403

        denied_with_header = client.put(
            f"/admin/tenants/{tenant_b_id}/members/member_f",
            headers=_auth_header(admin_token, tenant_b_id),
            json={"role": "member", "active": True, "is_default": False},
        )
        assert denied_with_header.status_code == 403

    engine.dispose()


def test_non_admin_user_denied_membership_management(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_user_denied.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="team-g", name="Team G", active=True)
        tenant_id = str(tenant.id)
        repo.upsert_auth_user(
            username="user_g",
            password_hash=hash_password_bcrypt("user12345"),
            role="user",
            active=True,
            tenant_id=tenant_id,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", True)

    with TestClient(main.app) as client:
        user_token = _login(client, "user_g", "user12345")
        denied = client.get(
            f"/admin/tenants/{tenant_id}/members",
            headers=_auth_header(user_token),
        )
        assert denied.status_code == 403

    engine.dispose()


def test_membership_api_flag_off_behavior_unchanged(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_membership_flag_off.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        tenant = repo.create_tenant(code="team-h", name="Team H", active=True)
        tenant_id = str(tenant.id)
        repo.upsert_auth_user(
            username="root",
            password_hash=hash_password_bcrypt("root12345"),
            role="root",
            active=True,
        )
        repo.upsert_auth_user(
            username="admin_h",
            password_hash=hash_password_bcrypt("admin12345"),
            role="admin",
            active=True,
            tenant_id=tenant_id,
        )

    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main, "FEATURE_MULTI_TENANT_FOUNDATION", False)

    with TestClient(main.app) as client:
        root_token = _login(client, "root", "root12345")
        root_disabled = client.get(
            f"/admin/tenants/{tenant_id}/members",
            headers=_auth_header(root_token),
        )
        assert root_disabled.status_code == 404
        assert "feature disabled" in str(root_disabled.json().get("detail") or "")

        admin_token = _login(client, "admin_h", "admin12345")
        admin_disabled = client.get(
            f"/admin/tenants/{tenant_id}/members",
            headers=_auth_header(admin_token),
        )
        assert admin_disabled.status_code == 404
        assert "feature disabled" in str(admin_disabled.json().get("detail") or "")

        users = client.get("/admin/users", headers=_auth_header(root_token))
        assert users.status_code == 200, users.text

    engine.dispose()
