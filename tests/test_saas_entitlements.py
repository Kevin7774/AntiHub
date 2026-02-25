from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import billing.entitlements as entitlements_module
import main
from billing import BillingRepository, build_session_factory, init_billing_db, session_scope
from billing.entitlements import (
    get_user_entitlements,
    invalidate_user_entitlements,
    require_entitlement,
)
from billing.db import init_billing_db as billing_init_billing_db
from billing.db import session_scope as billing_session_scope


def _make_db(tmp_path: Path):
    db_path = tmp_path / "saas_entitlements.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    init_billing_db(engine)
    return engine, session_factory


def _seed_user_plan(session_factory, *, username: str = "alice") -> tuple[str, str, str]:
    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        tenant = repo.get_or_create_tenant(code="default", name="Default Tenant", active=True)
        repo.upsert_auth_user(
            username=username,
            password_hash="test_hash",
            role="user",
            active=True,
            tenant_id=str(tenant.id),
        )
        plan = repo.create_plan(
            code=f"pro_{username}",
            name="Pro",
            price_cents=9900,
            monthly_points=1000,
            billing_cycle="monthly",
            trial_days=7,
            metadata_json={"tier": "pro"},
        )
        entitlement = repo.create_plan_entitlement(
            plan_id=str(plan.id),
            key="feature.deep_search",
            enabled=True,
            value_json={"mode": "deep"},
            limit_value=50,
            metadata_json={"origin": "test"},
        )
        sub = repo.bind_user_plan(user_id=username, plan_id=str(plan.id), duration_days=30)
        return str(plan.id), str(entitlement.id), str(sub.id)


def test_create_plan_and_entitlement(tmp_path: Path) -> None:
    engine, session_factory = _make_db(tmp_path)
    try:
        with session_scope(session_factory) as session:
            repo = BillingRepository(session)
            plan = repo.create_plan(
                code="team",
                name="Team",
                price_cents=19900,
                monthly_points=3000,
                billing_cycle="yearly",
                trial_days=14,
                metadata_json={"segment": "b2b"},
            )
            entitlement = repo.create_plan_entitlement(
                plan_id=str(plan.id),
                key="feature.analytics",
                enabled=True,
                value_json={"level": "advanced"},
                limit_value=100,
                metadata_json={"unit": "projects"},
            )
        with session_scope(session_factory) as session:
            repo = BillingRepository(session)
            fetched_plan = repo.get_plan_by_id(str(plan.id))
            fetched_entitlements = repo.list_plan_entitlements(plan_id=str(plan.id), include_disabled=True)
            assert fetched_plan is not None
            assert str(getattr(fetched_plan, "billing_cycle", "")) == "yearly"
            assert int(getattr(fetched_plan, "trial_days", 0) or 0) == 14
            assert isinstance(getattr(fetched_plan, "metadata_json", {}), dict)
            assert any(str(item.id) == str(entitlement.id) for item in fetched_entitlements)
    finally:
        engine.dispose()


def test_bind_user_plan(tmp_path: Path) -> None:
    engine, session_factory = _make_db(tmp_path)
    try:
        plan_id, _entitlement_id, sub_id = _seed_user_plan(session_factory, username="bind_user")
        with session_scope(session_factory) as session:
            repo = BillingRepository(session)
            sub = repo.get_active_subscription("bind_user")
            assert sub is not None
            assert str(sub.id) == sub_id
            assert str(sub.plan_id) == plan_id
    finally:
        engine.dispose()


def test_get_user_entitlements_returns_expected(tmp_path: Path) -> None:
    engine, session_factory = _make_db(tmp_path)
    try:
        _seed_user_plan(session_factory, username="ent_user")
        result = get_user_entitlements("ent_user", session_factory=session_factory, force_refresh=True)
        assert "feature.deep_search" in result
        item = result["feature.deep_search"]
        assert item["enabled"] is True
        assert item["limit"] == 50
        assert item["value"] == {"mode": "deep"}
    finally:
        invalidate_user_entitlements("ent_user")
        engine.dispose()


def test_require_entitlement_reject_path(monkeypatch) -> None:
    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):
        request.state.auth_identity = SimpleNamespace(username="alice")
        return await call_next(request)

    @app.get("/protected")
    async def _protected(_: dict = Depends(require_entitlement("feature.deep_search"))):
        return {"ok": True}

    monkeypatch.setattr(entitlements_module, "FEATURE_SAAS_ENTITLEMENTS", True)
    monkeypatch.setattr(entitlements_module, "get_user_entitlements", lambda _uid: {})

    with TestClient(app) as client:
        resp = client.get("/protected")
        assert resp.status_code == 403


def test_require_entitlement_pass_path(monkeypatch) -> None:
    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):
        request.state.auth_identity = SimpleNamespace(username="alice")
        return await call_next(request)

    @app.get("/protected")
    async def _protected(_: dict = Depends(require_entitlement("feature.deep_search"))):
        return {"ok": True}

    monkeypatch.setattr(entitlements_module, "FEATURE_SAAS_ENTITLEMENTS", True)
    monkeypatch.setattr(
        entitlements_module,
        "get_user_entitlements",
        lambda _uid: {"feature.deep_search": {"enabled": True, "value": {"mode": "deep"}, "limit": 10}},
    )

    with TestClient(app) as client:
        resp = client.get("/protected")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


def test_entitlements_cache_invalidation(tmp_path: Path) -> None:
    engine, session_factory = _make_db(tmp_path)
    try:
        plan_id, entitlement_id, _sub_id = _seed_user_plan(session_factory, username="cache_user")
        first = get_user_entitlements("cache_user", session_factory=session_factory, force_refresh=True)
        assert first["feature.deep_search"]["limit"] == 50

        with session_scope(session_factory) as session:
            repo = BillingRepository(session)
            repo.update_plan_entitlement(entitlement_id, limit_value=120)

        # without invalidation, cache should still return old limit
        cached = get_user_entitlements("cache_user", session_factory=session_factory)
        assert cached["feature.deep_search"]["limit"] == 50

        entitlements_module.invalidate_plan_entitlements(plan_id, session_factory=session_factory)
        refreshed = get_user_entitlements("cache_user", session_factory=session_factory)
        assert refreshed["feature.deep_search"]["limit"] == 120
    finally:
        invalidate_user_entitlements("cache_user")
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_saas_admin_api_and_entitlements_me(monkeypatch, tmp_path: Path) -> None:
    engine, session_factory = _make_db(tmp_path)

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "saas-secret")
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
    monkeypatch.setattr(main, "FEATURE_SAAS_ENTITLEMENTS", True)
    monkeypatch.setattr(main, "FEATURE_SAAS_ADMIN_API", True)
    monkeypatch.setattr(main, "session_scope", _session_scope_override)
    monkeypatch.setattr(main, "init_billing_db", lambda: billing_init_billing_db(engine))
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)

    try:
        with TestClient(main.app) as client:
            admin_token = _login(client, "admin", "admin123")
            user_token = _login(client, "alice", "alice123")

            plan_resp = client.post(
                "/admin/saas/plans",
                headers=_auth_header(admin_token),
                json={
                    "code": "pro_saas_api",
                    "name": "Pro SaaS API",
                    "currency": "usd",
                    "price_cents": 29900,
                    "monthly_points": 5000,
                    "billing_cycle": "monthly",
                    "trial_days": 7,
                    "metadata": {"segment": "self-serve"},
                    "active": True,
                },
            )
            assert plan_resp.status_code == 200, plan_resp.text
            plan_id = str(plan_resp.json()["plan_id"])

            ent_resp = client.post(
                f"/admin/saas/plans/{plan_id}/entitlements",
                headers=_auth_header(admin_token),
                json={
                    "key": "feature.deep_search",
                    "enabled": True,
                    "value": {"mode": "deep"},
                    "limit": 88,
                    "metadata": {"unit": "requests/day"},
                },
            )
            assert ent_resp.status_code == 200, ent_resp.text

            bind_resp = client.post(
                "/admin/saas/users/alice/plan",
                headers=_auth_header(admin_token),
                json={"plan_id": plan_id, "duration_days": 30},
            )
            assert bind_resp.status_code == 200, bind_resp.text
            assert bind_resp.json()["plan_code"] == "pro_saas_api"

            me_resp = client.get("/billing/entitlements/me", headers=_auth_header(user_token))
            assert me_resp.status_code == 200, me_resp.text
            entitlements = me_resp.json()["entitlements"]
            assert "feature.deep_search" in entitlements
            assert entitlements["feature.deep_search"]["enabled"] is True
    finally:
        invalidate_user_entitlements("alice")
        engine.dispose()
