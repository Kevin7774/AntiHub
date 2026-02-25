from __future__ import annotations

from datetime import datetime, timedelta, timezone

from billing import (
    BillingRepository,
    build_session_factory,
    init_billing_db,
    session_scope,
)
from billing.models import OrderStatus
from billing.service import close_timed_out_orders, process_payment_webhook


def make_db():
    engine, session_factory = build_session_factory("sqlite+pysqlite:///:memory:")
    init_billing_db(engine)
    return engine, session_factory


def test_timeout_closes_stale_pending_orders() -> None:
    engine, session_factory = make_db()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="pro", name="Pro", price_cents=9900, monthly_points=1000)
        order = repo.create_order(
            user_id="alice",
            plan_id=plan.id,
            amount_cents=9900,
            currency="usd",
            provider="mockpay",
            external_order_id="ord_timeout_001",
            idempotency_key="order-timeout-1",
        )
        # Make it stale.
        order.created_at = now - timedelta(hours=2)
        session.flush()

        closed = close_timed_out_orders(repo, timeout_seconds=3600, now=now)
        assert closed == 1
        assert order.status == OrderStatus.CANCELED

    engine.dispose()


def test_refund_rolls_back_points_and_is_idempotent() -> None:
    engine, session_factory = make_db()

    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="pro", name="Pro", price_cents=9900, monthly_points=1000)
        repo.create_order(
            user_id="alice",
            plan_id=plan.id,
            amount_cents=9900,
            currency="usd",
            provider="mockpay",
            external_order_id="ord_ref_001",
            idempotency_key="checkout:alice:ord_ref_001",
        )

        paid_event = {
            "event_type": "payment.succeeded",
            "event_id": "evt_paid_1",
            "provider": "mockpay",
            "data": {
                "user_id": "alice",
                "plan_code": "pro",
                "external_order_id": "ord_ref_001",
                "amount_cents": 9900,
                "currency": "usd",
                "duration_days": 30,
            },
        }
        result = process_payment_webhook(repo, paid_event)
        assert result["status"] == "processed"
        assert repo.get_user_point_balance("alice") == 1000

        refund_event_1 = {
            "event_type": "payment.refunded",
            "event_id": "evt_refund_1",
            "provider": "mockpay",
            "data": {"external_order_id": "ord_ref_001"},
        }
        refunded = process_payment_webhook(repo, refund_event_1)
        assert refunded["status"] == "refunded"
        assert repo.get_user_point_balance("alice") == 0

        # Idempotent even when a provider sends a different refund event id.
        refund_event_2 = {
            "event_type": "payment.refunded",
            "event_id": "evt_refund_2",
            "provider": "mockpay",
            "data": {"external_order_id": "ord_ref_001"},
        }
        refunded_again = process_payment_webhook(repo, refund_event_2)
        assert refunded_again["status"] == "refunded"
        assert repo.get_user_point_balance("alice") == 0

    engine.dispose()


def test_upgrade_resets_expiry_and_grants_delta_points() -> None:
    engine, session_factory = make_db()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        basic = repo.create_plan(code="basic_monthly", name="Basic", price_cents=19800, monthly_points=1000)
        pro = repo.create_plan(code="pro_monthly", name="Pro", price_cents=39800, monthly_points=3000)

        order_basic = repo.create_order(
            user_id="alice",
            plan_id=basic.id,
            amount_cents=19800,
            currency="cny",
            provider="mockpay",
            external_order_id="ord_upgrade_basic",
            idempotency_key="checkout:alice:upgrade-basic",
        )
        event_basic = {
            "event_type": "payment.succeeded",
            "event_id": "evt_upgrade_basic",
            "provider": "mockpay",
            "data": {
                "external_order_id": "ord_upgrade_basic",
                "amount_cents": 19800,
                "currency": "cny",
                "paid_at": now.isoformat(),
            },
        }
        first = process_payment_webhook(repo, event_basic)
        assert first["status"] == "processed"
        assert first["upgrade_applied"] is False
        assert first["points_granted"] == 1000
        subscription = repo.get_active_subscription("alice", now=now)
        assert subscription is not None
        assert subscription.expires_at.date() == (now + timedelta(days=30)).date()
        assert repo.get_user_point_balance("alice") == 1000

        # Upgrade while still active: reset expiry and grant delta points only.
        upgrade_time = now + timedelta(days=10)
        order_pro = repo.create_order(
            user_id="alice",
            plan_id=pro.id,
            amount_cents=39800,
            currency="cny",
            provider="mockpay",
            external_order_id="ord_upgrade_pro",
            idempotency_key="checkout:alice:upgrade-pro",
        )
        event_pro = {
            "event_type": "payment.succeeded",
            "event_id": "evt_upgrade_pro",
            "provider": "mockpay",
            "data": {
                "external_order_id": "ord_upgrade_pro",
                "amount_cents": 39800,
                "currency": "cny",
                "paid_at": upgrade_time.isoformat(),
            },
        }
        second = process_payment_webhook(repo, event_pro)
        assert second["status"] == "processed"
        assert second["upgrade_applied"] is True
        assert second["points_granted"] == 2000

        upgraded = repo.get_active_subscription("alice", now=upgrade_time)
        assert upgraded is not None
        assert upgraded.plan_id == pro.id
        assert upgraded.expires_at.date() == (upgrade_time + timedelta(days=30)).date()
        assert repo.get_user_point_balance("alice") == 3000
        assert order_basic.id != order_pro.id

    engine.dispose()
