from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import main
from billing.db import build_session_factory
from billing.db import init_billing_db as billing_init_billing_db
from billing.db import session_scope as billing_session_scope
from recommend.models import RecommendationResponse


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    data = response.json()
    return str(data["access_token"])


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_auth_rbac_and_webhook_idempotency(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "auth_billing.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

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
    monkeypatch.setattr(main, "AUTH_TOKEN_TTL_SECONDS", 7200)
    monkeypatch.setattr(main, "PAYMENT_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(main, "session_scope", _session_scope_override)
    monkeypatch.setattr(main, "init_billing_db", lambda: billing_init_billing_db(engine))
    monkeypatch.setattr(main, "DEEP_SEARCH_POINTS_COST", 50)

    def _fake_recommend_products(**_kwargs):
        return RecommendationResponse(
            request_id="rec_test_001",
            query="deep",
            mode="deep",
            generated_at=0.0,
            warnings=[],
            recommendations=[],
        )

    monkeypatch.setattr(main, "recommend_products", _fake_recommend_products)

    with TestClient(main.app) as client:
        unauth = client.get("/error-codes")
        assert unauth.status_code == 401

        admin_token = _login(client, "admin", "admin123")
        user_token = _login(client, "alice", "alice123")

        me = client.get("/auth/me", headers=_auth_header(user_token))
        assert me.status_code == 200
        assert me.json()["username"] == "alice"
        assert me.json()["role"] == "user"

        forbidden_plan = client.post(
            "/admin/billing/plans",
            headers=_auth_header(user_token),
            json={
                "code": "pro",
                "name": "Pro",
                "price_cents": 9900,
                "monthly_points": 1000,
                "currency": "usd",
            },
        )
        assert forbidden_plan.status_code == 403

        created_plan = client.post(
            "/admin/billing/plans",
            headers=_auth_header(admin_token),
            json={
                "code": "pro",
                "name": "Pro",
                "price_cents": 9900,
                "monthly_points": 1000,
                "currency": "usd",
            },
        )
        assert created_plan.status_code == 200
        assert created_plan.json()["code"] == "pro"
        plan_id = str(created_plan.json()["plan_id"])

        forbidden_update = client.put(
            f"/admin/billing/plans/{plan_id}",
            headers=_auth_header(user_token),
            json={"price_cents": 10900, "monthly_points": 1500},
        )
        assert forbidden_update.status_code == 403

        updated = client.put(
            f"/admin/billing/plans/{plan_id}",
            headers=_auth_header(admin_token),
            json={"price_cents": 10900, "monthly_points": 1500},
        )
        assert updated.status_code == 200
        assert updated.json()["price_cents"] == 10900
        assert updated.json()["monthly_points"] == 1500

        deep_forbidden = client.post(
            "/recommendations",
            headers=_auth_header(user_token),
            data={"query": "CRM", "mode": "deep", "limit": "10"},
        )
        assert deep_forbidden.status_code == 402

        checkout = client.post(
            "/billing/checkout",
            headers=_auth_header(user_token),
            json={"plan_code": "pro", "idempotency_key": "checkout-1"},
        )
        assert checkout.status_code == 200, checkout.text
        external_order_id = str(checkout.json()["external_order_id"])
        checkout_url = str(checkout.json()["checkout_url"])
        assert checkout.json()["provider"] == "mock"
        assert external_order_id
        assert checkout_url

        with _session_scope_override() as session:
            repo = main.BillingRepository(session)
            order = repo.get_order_by_external_order_id(external_order_id)
            assert order is not None
            assert order.provider_payload
            payload = json.loads(order.provider_payload)
            assert isinstance(payload, dict)
            assert payload.get("checkout", {}).get("checkout_url") == checkout_url

        checkout_repeat = client.post(
            "/billing/checkout",
            headers=_auth_header(user_token),
            json={"plan_code": "pro", "idempotency_key": "checkout-1"},
        )
        assert checkout_repeat.status_code == 200, checkout_repeat.text
        assert checkout_repeat.json()["external_order_id"] == external_order_id
        order_pending = client.get(f"/billing/orders/me/{external_order_id}/status", headers=_auth_header(user_token))
        assert order_pending.status_code == 200, order_pending.text
        assert order_pending.json()["external_order_id"] == external_order_id
        assert order_pending.json()["status"] == "pending"

        event = {
            "event_type": "payment.succeeded",
            "event_id": "evt_1",
            "provider": "mockpay",
            "data": {
                "user_id": "alice",
                "plan_code": "pro",
                "external_order_id": external_order_id,
                "amount_cents": 10900,
                "currency": "usd",
                "duration_days": 30,
            },
        }
        payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
        signature = _sign(payload, "whsec_test")

        bad_signature = client.post(
            "/billing/webhooks/payment",
            content=payload,
            headers={"Content-Type": "application/json", "X-Signature": "bad"},
        )
        assert bad_signature.status_code == 403

        first = client.post(
            "/billing/webhooks/payment",
            content=payload,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "processed"

        second = client.post(
            "/billing/webhooks/payment",
            content=payload,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "processed"
        order_paid = client.get(f"/billing/orders/me/{external_order_id}/status", headers=_auth_header(user_token))
        assert order_paid.status_code == 200, order_paid.text
        assert order_paid.json()["status"] == "paid"
        assert order_paid.json()["paid_at"] is not None

        with _session_scope_override() as session:
            repo = main.BillingRepository(session)
            order = repo.get_order_by_external_order_id(external_order_id)
            assert order is not None
            assert order.provider_payload
            merged = json.loads(order.provider_payload)
            assert isinstance(merged, dict)
            assert merged.get("checkout", {}).get("checkout_url") == checkout_url
            assert isinstance(merged.get("webhook"), dict)
            assert merged["webhook"].get("event_id") == "evt_1"

        sub = client.get("/billing/subscription/me", headers=_auth_header(user_token))
        assert sub.status_code == 200
        assert sub.json()["status"] == "active"
        assert sub.json()["plan_code"] == "pro"

        points = client.get("/billing/points/me", headers=_auth_header(user_token))
        assert points.status_code == 200
        assert points.json()["balance"] == 1500

        deep_ok = client.post(
            "/recommendations",
            headers=_auth_header(user_token),
            data={"query": "CRM", "mode": "deep", "limit": "10"},
        )
        assert deep_ok.status_code == 200, deep_ok.text

        points_after_deep = client.get("/billing/points/me", headers=_auth_header(user_token))
        assert points_after_deep.status_code == 200
        assert points_after_deep.json()["balance"] == 1450

        point_history = client.get("/billing/points/history/me", headers=_auth_header(user_token))
        assert point_history.status_code == 200
        history_rows = point_history.json()
        assert isinstance(history_rows, list)
        assert any(str(row.get("note") or "").startswith("deep_search:") and int(row.get("points") or 0) == -50 for row in history_rows)

        forbidden_user_status = client.get("/admin/billing/users/status", headers=_auth_header(user_token))
        assert forbidden_user_status.status_code == 403

        user_status = client.get(
            "/admin/billing/users/status",
            headers=_auth_header(admin_token),
            params={"username": "alice"},
        )
        assert user_status.status_code == 200, user_status.text
        user_items = user_status.json()
        assert isinstance(user_items, list)
        assert len(user_items) == 1
        assert user_items[0]["username"] == "alice"
        assert user_items[0]["subscription"]["status"] == "active"
        assert user_items[0]["subscription"]["plan_code"] == "pro"
        assert user_items[0]["points_balance"] == 1450

        forbidden_orders = client.get("/admin/billing/orders", headers=_auth_header(user_token))
        assert forbidden_orders.status_code == 403

        orders = client.get("/admin/billing/orders", headers=_auth_header(admin_token))
        assert orders.status_code == 200
        items = orders.json()
        assert isinstance(items, list)
        assert any(item.get("external_order_id") == external_order_id for item in items)

        forbidden_audit = client.get("/admin/billing/audit", headers=_auth_header(user_token))
        assert forbidden_audit.status_code == 403

        audit = client.get(
            "/admin/billing/audit",
            headers=_auth_header(admin_token),
            params={"external_order_id": external_order_id},
        )
        assert audit.status_code == 200
        logs = audit.json()
        assert isinstance(logs, list)
        assert any(log.get("external_order_id") == external_order_id for log in logs)
        assert any(log.get("outcome") == "processed" for log in logs)

        log_id = str(logs[0]["log_id"])
        detail = client.get(f"/admin/billing/audit/{log_id}", headers=_auth_header(admin_token))
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert detail_payload.get("log_id") == log_id
        assert "raw_payload" in detail_payload

    engine.dispose()


def test_wechatpay_webhook_concurrent_replay_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "wechatpay_concurrent.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(main, "PAYMENT_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(main, "session_scope", _session_scope_override)
    monkeypatch.setattr(main, "init_billing_db", lambda: billing_init_billing_db(engine))
    monkeypatch.setattr(main, "WECHATPAY_APIV3_KEY", "a" * 32)
    monkeypatch.setattr(main, "parse_platform_certs", lambda **_kwargs: {"serial_x": "pem"})
    monkeypatch.setattr(main, "verify_wechatpay_notify_signature", lambda **_kwargs: True)

    external_order_id = "ord_wechatpay_concurrent_001"
    success_time = datetime.now(timezone.utc).isoformat()

    def _fake_decrypt_notification(*, api_v3_key: str, resource: dict):  # noqa: ARG001
            return {
                "out_trade_no": external_order_id,
                "trade_state": "SUCCESS",
                "amount": {"total": 9900, "currency": "CNY"},
                "success_time": success_time,
                "transaction_id": "tx_concurrent_001",
            }

    monkeypatch.setattr(main, "decrypt_notification", _fake_decrypt_notification)

    with _session_scope_override() as session:
        repo = main.BillingRepository(session)
        plan = repo.create_plan(code="wechat_pro", name="WeChat Pro", price_cents=9900, monthly_points=1500, currency="cny")
        repo.create_order(
            user_id="alice",
            plan_id=plan.id,
            amount_cents=9900,
            currency="cny",
            provider="wechatpay",
            external_order_id=external_order_id,
            idempotency_key="checkout:alice:wechat-concurrent",
        )

    body = {
        "id": "evt_wechat_concurrent",
        "event_type": "TRANSACTION.SUCCESS",
        "resource": {
            "algorithm": "AEAD_AES_256_GCM",
            "nonce": "nonce",
            "associated_data": "transaction",
            "ciphertext": "cipher",
        },
    }
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Wechatpay-Timestamp": "1700000000",
        "Wechatpay-Nonce": "nonce",
        "Wechatpay-Signature": "sig",
        "Wechatpay-Serial": "serial_x",
    }

    with TestClient(main.app) as client:
        statuses: list[int] = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(client.post, "/billing/webhooks/wechatpay", content=payload, headers=headers) for _ in range(20)]
            for future in as_completed(futures):
                response = future.result()
                statuses.append(response.status_code)

    assert statuses
    assert all(code == 200 for code in statuses)

    with _session_scope_override() as session:
        repo = main.BillingRepository(session)
        points = repo.get_user_point_balance("alice")
        assert points == 1500
        subscription = repo.get_active_subscription("alice")
        assert subscription is not None
        assert str(subscription.plan.code) == "wechat_pro"

    engine.dispose()
