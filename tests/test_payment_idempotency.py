"""Regression tests for payment callback idempotency and order state machine.

These tests verify that duplicate callback replays do NOT grant entitlements
(points / subscriptions) twice, and that order state transitions are
deterministic and guarded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from billing import (
    BillingRepository,
    build_session_factory,
    init_billing_db,
    session_scope,
)
from billing.models import OrderStatus, PointFlowType
from billing.repository import BillingStateError
from billing.service import PaymentWebhookError, process_payment_webhook


def _make_db():
    engine, session_factory = build_session_factory("sqlite+pysqlite:///:memory:")
    init_billing_db(engine)
    return engine, session_factory


# ---------------------------------------------------------------------------
# 1. Duplicate callback replay must NOT grant entitlements twice
# ---------------------------------------------------------------------------


def test_duplicate_payment_callback_does_not_double_grant() -> None:
    """Core idempotency guarantee: replaying the same payment.succeeded
    event (even with a different event_id) must not create a second
    point grant or subscription."""

    engine, sf = _make_db()

    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="pro_monthly", name="Pro", price_cents=19800, monthly_points=1500)
        repo.create_order(
            user_id="bob",
            plan_id=plan.id,
            amount_cents=19800,
            currency="cny",
            provider="mockpay",
            external_order_id="ord_replay_001",
            idempotency_key="checkout:bob:replay-001",
        )

        event_first = {
            "event_type": "payment.succeeded",
            "event_id": "evt_first",
            "provider": "mockpay",
            "data": {
                "external_order_id": "ord_replay_001",
                "amount_cents": 19800,
                "currency": "cny",
            },
        }
        result1 = process_payment_webhook(repo, event_first)
        assert result1["status"] == "processed"
        assert result1["points_granted"] == 1500
        assert repo.get_user_point_balance("bob") == 1500

        # Replay with SAME event_id (exact duplicate).
        result2 = process_payment_webhook(repo, event_first)
        assert result2["status"] == "processed"
        assert result2["points_granted"] == 1500
        assert repo.get_user_point_balance("bob") == 1500  # unchanged

        # Replay with DIFFERENT event_id (provider retransmit).
        event_retransmit = {
            "event_type": "payment.succeeded",
            "event_id": "evt_retransmit",
            "provider": "mockpay",
            "data": {
                "external_order_id": "ord_replay_001",
                "amount_cents": 19800,
                "currency": "cny",
            },
        }
        result3 = process_payment_webhook(repo, event_retransmit)
        assert result3["status"] == "processed"
        assert result3["points_granted"] == 1500
        assert repo.get_user_point_balance("bob") == 1500  # still unchanged

        # Verify only ONE point flow was created.
        flows = repo.list_point_flows(user_id="bob")
        grant_flows = [f for f in flows if f.flow_type == PointFlowType.GRANT]
        assert len(grant_flows) == 1

    engine.dispose()


# ---------------------------------------------------------------------------
# 2. Order state machine: valid transitions
# ---------------------------------------------------------------------------


def test_order_transitions_pending_to_paid() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="t1")
        assert order.status == OrderStatus.PENDING
        repo.mark_order_paid(order.id)
        assert order.status == OrderStatus.PAID
    engine.dispose()


def test_order_transitions_pending_to_canceled() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="t2")
        repo.mark_order_canceled(order.id)
        assert order.status == OrderStatus.CANCELED
    engine.dispose()


def test_order_transitions_pending_to_failed() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="t3")
        repo.mark_order_failed(order.id)
        assert order.status == OrderStatus.FAILED
    engine.dispose()


def test_order_transitions_paid_to_refunded() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="t4")
        repo.mark_order_paid(order.id)
        repo.mark_order_refunded(order.id)
        assert order.status == OrderStatus.REFUNDED
    engine.dispose()


# ---------------------------------------------------------------------------
# 3. Order state machine: invalid transitions raise BillingStateError
# ---------------------------------------------------------------------------


def test_paid_order_cannot_be_canceled() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="inv1")
        repo.mark_order_paid(order.id)
        with pytest.raises(BillingStateError):
            repo.mark_order_canceled(order.id)
    engine.dispose()


def test_paid_order_cannot_be_failed() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="inv2")
        repo.mark_order_paid(order.id)
        with pytest.raises(BillingStateError):
            repo.mark_order_failed(order.id)
    engine.dispose()


def test_canceled_order_cannot_be_paid() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="inv3")
        repo.mark_order_canceled(order.id)
        with pytest.raises(BillingStateError):
            repo.mark_order_paid(order.id)
    engine.dispose()


def test_failed_order_cannot_be_paid() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="inv4")
        repo.mark_order_failed(order.id)
        with pytest.raises(BillingStateError):
            repo.mark_order_paid(order.id)
    engine.dispose()


def test_pending_order_cannot_be_refunded() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="inv5")
        with pytest.raises(BillingStateError):
            repo.mark_order_refunded(order.id)
    engine.dispose()


# ---------------------------------------------------------------------------
# 4. Idempotent terminal state transitions (re-entry returns same order)
# ---------------------------------------------------------------------------


def test_mark_order_paid_idempotent() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="idem1")
        repo.mark_order_paid(order.id)
        # Calling again returns same order, no error.
        same = repo.mark_order_paid(order.id)
        assert same.id == order.id
        assert same.status == OrderStatus.PAID
    engine.dispose()


def test_mark_order_failed_idempotent() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        order = repo.create_order("u1", plan.id, 100, external_order_id="idem2")
        repo.mark_order_failed(order.id)
        same = repo.mark_order_failed(order.id)
        assert same.id == order.id
        assert same.status == OrderStatus.FAILED
    engine.dispose()


# ---------------------------------------------------------------------------
# 5. Failure event via service layer
# ---------------------------------------------------------------------------


def test_payment_failed_event_closes_order() -> None:
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        repo.create_order("u1", plan.id, 100, external_order_id="fail1", provider="mockpay")
        event = {
            "event_type": "payment.failed",
            "event_id": "evt_fail_1",
            "provider": "mockpay",
            "data": {"external_order_id": "fail1"},
        }
        result = process_payment_webhook(repo, event)
        assert result["status"] == "failed"
        order = repo.get_order_by_external_order_id("fail1")
        assert order is not None
        assert order.status == OrderStatus.FAILED
    engine.dispose()


def test_payment_failed_does_not_affect_already_paid() -> None:
    """A stale failure callback arriving after payment succeeded must be ignored."""
    engine, sf = _make_db()
    with session_scope(sf) as session:
        repo = BillingRepository(session)
        plan = repo.create_plan(code="basic", name="Basic", price_cents=100, monthly_points=10)
        repo.create_order("u1", plan.id, 100, external_order_id="fail2", provider="mockpay")

        # First: pay successfully.
        pay_event = {
            "event_type": "payment.succeeded",
            "event_id": "evt_pay",
            "provider": "mockpay",
            "data": {"external_order_id": "fail2", "amount_cents": 100, "currency": "usd"},
        }
        process_payment_webhook(repo, pay_event)

        # Then: stale failure arrives.
        fail_event = {
            "event_type": "payment.failed",
            "event_id": "evt_fail_stale",
            "provider": "mockpay",
            "data": {"external_order_id": "fail2"},
        }
        result = process_payment_webhook(repo, fail_event)
        assert result["status"] == "ignored"
        assert result["reason"] == "order already paid"

        order = repo.get_order_by_external_order_id("fail2")
        assert order is not None
        assert order.status == OrderStatus.PAID
    engine.dispose()
