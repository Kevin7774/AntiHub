from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

import main
from runtime_metrics import record_counter_metric, record_timing_metric


def test_runtime_metrics_endpoint_is_admin_only_and_returns_snapshot(monkeypatch) -> None:
    @contextmanager
    def _noop_session_scope():
        yield None

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
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 3600)
    monkeypatch.setattr(main, "session_scope", _noop_session_scope)
    monkeypatch.setattr(main, "init_billing_db", lambda: None)
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)
    record_counter_metric(name="recommend.llm.tokens.total", value=123)
    record_timing_metric(name="recommend.provider.github.latency_ms", duration_ms=45)

    with TestClient(main.app) as client:
        user_login = client.post("/auth/login", json={"username": "alice", "password": "alice123"})
        assert user_login.status_code == 200, user_login.text
        user_token = str(user_login.json()["access_token"])

        forbidden = client.get("/metrics/runtime", headers={"Authorization": f"Bearer {user_token}"})
        assert forbidden.status_code == 403

        admin_login = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        assert admin_login.status_code == 200, admin_login.text
        admin_token = str(admin_login.json()["access_token"])

        snapshot = client.get("/metrics/runtime", headers={"Authorization": f"Bearer {admin_token}"})
        assert snapshot.status_code == 200, snapshot.text
        payload = snapshot.json()
        assert "requests_total" in payload
        assert "errors_5xx_total" in payload
        assert "status_counts" in payload
        assert "custom_counters" in payload
        assert "custom_timers" in payload
        assert int(payload["custom_counters"].get("recommend.llm.tokens.total") or 0) >= 123
        assert "recommend.provider.github.latency_ms" in payload["custom_timers"]
