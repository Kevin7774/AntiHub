from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import main
from billing import BillingRateLimiter, resolve_plan_rpm
from recommend.models import RecommendationResponse


def test_resolve_plan_rpm_mapping() -> None:
    assert resolve_plan_rpm("") == 5
    assert resolve_plan_rpm("vip_monthly") == 50
    assert resolve_plan_rpm("vip_quarterly") == 100
    assert resolve_plan_rpm("vip_yearly") == 500
    assert resolve_plan_rpm("paid_unknown") == 50


def test_recommendations_rate_limit(monkeypatch) -> None:
    @contextmanager
    def _noop_session_scope():
        yield None

    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(
        main,
        "AUTH_USERS_JSON",
        json.dumps({"alice": {"password": "alice123", "role": "user"}}),
    )
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 3600)
    monkeypatch.setattr(main, "session_scope", _noop_session_scope)
    monkeypatch.setattr(main, "resolve_user_rpm", lambda _username: 2)

    limiter = BillingRateLimiter()
    limiter._client = None  # use deterministic in-memory bucket for tests
    monkeypatch.setattr(main, "BILLING_RATE_LIMITER", limiter)

    def _fake_recommend_products(**kwargs):
        _ = kwargs
        return RecommendationResponse(
            request_id="req_test",
            query="CRM",
            mode="quick",
            generated_at=0.0,
            recommendations=[],
        )

    monkeypatch.setattr(main, "recommend_products", _fake_recommend_products)

    with TestClient(main.app) as client:
        login = client.post("/auth/login", json={"username": "alice", "password": "alice123"})
        assert login.status_code == 200, login.text
        token = str(login.json()["access_token"])
        headers = {"Authorization": f"Bearer {token}"}

        first = client.post("/recommendations", headers=headers, data={"query": "CRM", "mode": "quick", "limit": "1"})
        assert first.status_code == 200, first.text
        assert first.headers.get("X-RateLimit-Limit") == "2"

        second = client.post("/recommendations", headers=headers, data={"query": "CRM", "mode": "quick", "limit": "1"})
        assert second.status_code == 200, second.text

        third = client.post("/recommendations", headers=headers, data={"query": "CRM", "mode": "quick", "limit": "1"})
        assert third.status_code == 429, third.text
        assert third.headers.get("Retry-After") is not None


def test_rate_limiter_requires_redis_in_production(monkeypatch) -> None:
    import billing.middleware as billing_middleware

    monkeypatch.setattr(billing_middleware, "APP_ENV", "production")
    monkeypatch.setattr(billing_middleware, "REDIS_DISABLED", True)
    monkeypatch.setattr(billing_middleware, "REDIS_URL", "memory://")
    with pytest.raises(RuntimeError, match="Redis"):
        billing_middleware.BillingRateLimiter()


def test_recommendations_rate_limit_returns_503_when_unavailable(monkeypatch) -> None:
    @contextmanager
    def _noop_session_scope():
        yield None

    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(
        main,
        "AUTH_USERS_JSON",
        json.dumps({"alice": {"password": "alice123", "role": "user"}}),
    )
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 3600)
    monkeypatch.setattr(main, "session_scope", _noop_session_scope)
    monkeypatch.setattr(main, "resolve_user_rpm", lambda _username: 2)

    class _BrokenLimiter:
        def allow(self, *, subject: str, limit_rpm: int, cost: int = 1):
            _ = (subject, limit_rpm, cost)
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(main, "BILLING_RATE_LIMITER", _BrokenLimiter())

    def _fake_recommend_products(**kwargs):
        _ = kwargs
        return RecommendationResponse(
            request_id="req_test",
            query="CRM",
            mode="quick",
            generated_at=0.0,
            recommendations=[],
        )

    monkeypatch.setattr(main, "recommend_products", _fake_recommend_products)

    with TestClient(main.app) as client:
        login = client.post("/auth/login", json={"username": "alice", "password": "alice123"})
        assert login.status_code == 200, login.text
        token = str(login.json()["access_token"])
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post("/recommendations", headers=headers, data={"query": "CRM", "mode": "quick", "limit": "1"})
        assert response.status_code == 503, response.text
