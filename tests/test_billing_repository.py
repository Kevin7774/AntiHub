from __future__ import annotations

from datetime import datetime, timedelta, timezone

from billing import (
    BillingRepository,
    PointFlowType,
    SubscriptionStatus,
    build_session_factory,
    init_billing_db,
    session_scope,
)


def make_db():
    engine, session_factory = build_session_factory("sqlite+pysqlite:///:memory:")
    init_billing_db(engine)
    return engine, session_factory


def test_order_idempotency_and_subscription_lifecycle() -> None:
    engine, session_factory = make_db()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="starter", name="Starter", price_cents=1999, monthly_points=300)
        first = repo.create_order(
            user_id="u_1",
            plan_id=plan.id,
            amount_cents=1999,
            idempotency_key="order-1",
        )
        second = repo.create_order(
            user_id="u_1",
            plan_id=plan.id,
            amount_cents=1999,
            idempotency_key="order-1",
        )
        assert first.id == second.id

        repo.mark_order_paid(first.id, paid_at=now)
        subscription = repo.activate_subscription_from_order(first.id, duration_days=30, now=now)
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.expires_at.date() == (now + timedelta(days=30)).date()

        expired = repo.expire_due_subscriptions(now=now + timedelta(days=31))
        assert expired == 1
        assert repo.get_active_subscription("u_1", now=now + timedelta(days=31)) is None

    engine.dispose()


def test_point_flow_balance_and_idempotency() -> None:
    engine, session_factory = make_db()

    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        grant = repo.record_point_flow(
            user_id="u_2",
            flow_type=PointFlowType.GRANT,
            points=100,
            idempotency_key="pf-1",
        )
        grant_repeat = repo.record_point_flow(
            user_id="u_2",
            flow_type=PointFlowType.GRANT,
            points=100,
            idempotency_key="pf-1",
        )
        assert grant.id == grant_repeat.id

        repo.record_point_flow(
            user_id="u_2",
            flow_type=PointFlowType.CONSUME,
            points=-30,
            idempotency_key="pf-2",
        )
        assert repo.get_user_point_balance("u_2") == 70

    engine.dispose()


def test_session_scope_rolls_back_on_error() -> None:
    engine, session_factory = make_db()

    try:
        with session_scope(session_factory) as session:
            repo = BillingRepository(session)
            repo.create_plan(code="pro", name="Pro", price_cents=9999, monthly_points=3000)
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        assert repo.get_plan_by_code("pro") is None

    engine.dispose()
