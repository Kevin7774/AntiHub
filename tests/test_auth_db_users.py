from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

import main
from auth import hash_password_bcrypt
from billing.db import build_session_factory
from billing.db import init_billing_db as billing_init_billing_db
from billing.db import session_scope as billing_session_scope


def test_auth_login_uses_db_users_when_auth_users_json_is_empty(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "auth_db_users.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        repo.upsert_auth_user(
            username="dbadmin",
            password_hash=hash_password_bcrypt("dbadmin123"),
            role="admin",
            active=True,
        )

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

    with TestClient(main.app) as client:
        login = client.post("/auth/login", json={"username": "dbadmin", "password": "dbadmin123"})
        assert login.status_code == 200, login.text
        token = str(login.json().get("access_token") or "")
        assert token

        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        payload = me.json()
        assert payload.get("username") == "dbadmin"
        assert payload.get("role") == "admin"

    engine.dispose()
