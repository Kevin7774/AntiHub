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


def _register(client: TestClient, username: str, password: str, tenant_name: str) -> str:
    response = client.post(
        "/auth/register",
        json={"username": username, "password": password, "tenant_name": tenant_name},
    )
    assert response.status_code == 200, response.text
    token = str(response.json().get("access_token") or "")
    assert token
    return token


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = str(response.json().get("access_token") or "")
    assert token
    return token


def test_case_tenant_scope_blocks_cross_tenant_reads(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "case_tenant_scope.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main.build_and_run, "delay", lambda *args, **kwargs: None)

    with TestClient(main.app) as client:
        alice_token = _register(client, "alice", "alice_12345", "Tenant A")
        bob_token = _register(client, "bob", "bob_12345", "Tenant B")

        created = client.post(
            "/cases",
            headers=_auth_header(alice_token),
            json={"repo_url": "https://example.com/demo.git", "run_mode": "showcase"},
        )
        assert created.status_code == 200, created.text
        payload = created.json()
        case_id = str(payload.get("case_id") or "")
        assert case_id
        assert payload.get("tenant_id")
        assert payload.get("owner_username") == "alice"

        alice_cases = client.get("/cases", headers=_auth_header(alice_token))
        assert alice_cases.status_code == 200, alice_cases.text
        assert any(item.get("case_id") == case_id for item in alice_cases.json().get("items", []))

        bob_cases = client.get("/cases", headers=_auth_header(bob_token))
        assert bob_cases.status_code == 200, bob_cases.text
        assert all(item.get("case_id") != case_id for item in bob_cases.json().get("items", []))

        denied = client.get(f"/cases/{case_id}", headers=_auth_header(bob_token))
        assert denied.status_code == 404

    engine.dispose()


def test_root_can_filter_cases_by_tenant(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "case_tenant_root_scope.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_auth_test_overrides(monkeypatch, session_factory, engine)
    monkeypatch.setattr(main.build_and_run, "delay", lambda *args, **kwargs: None)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        repo.upsert_auth_user(
            username="root",
            password_hash=hash_password_bcrypt("root12345"),
            role="root",
            active=True,
        )

    with TestClient(main.app) as client:
        alice_token = _register(client, "alice", "alice_12345", "Tenant A")
        me = client.get("/auth/me", headers=_auth_header(alice_token))
        assert me.status_code == 200, me.text
        tenant_id = str(me.json().get("tenant_id") or "")
        assert tenant_id

        created = client.post(
            "/cases",
            headers=_auth_header(alice_token),
            json={"repo_url": "https://example.com/demo.git", "run_mode": "showcase"},
        )
        assert created.status_code == 200, created.text
        case_id = str(created.json().get("case_id") or "")
        assert case_id

        root_token = _login(client, "root", "root12345")
        filtered = client.get("/cases", headers=_auth_header(root_token), params={"tenant_id": tenant_id})
        assert filtered.status_code == 200, filtered.text
        assert any(item.get("case_id") == case_id for item in filtered.json().get("items", []))

    engine.dispose()
