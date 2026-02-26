from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import math
from typing import Any, Final

from observability import get_logger, log_event

from .models import OrderStatus, PointFlowType
from .repository import BillingRepository, BillingStateError


class PaymentWebhookError(RuntimeError):
    pass


EVENT_TYPE_PAYMENT_SUCCEEDED: Final[str] = "payment.succeeded"
TIMEOUT_EVENT_TYPES: Final[frozenset[str]] = frozenset({"payment.timeout", "payment.timed_out", "order.timeout"})
REFUND_EVENT_TYPES: Final[frozenset[str]] = frozenset({"payment.refunded", "payment.refund"})

POINTS_GRANT_IDEMPOTENCY_PREFIX: Final[str] = "points-grant:order:"
POINTS_REFUND_IDEMPOTENCY_PREFIX: Final[str] = "points-refund:order:"
_LOGGER = get_logger("antihub.billing.service")


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify AntiHub internal webhook signature.

    - Algorithm: HMAC-SHA256(payload), returned as hex string.
    - Header format: accepts either a raw hex digest or "sha256=<hex>".
    - Constant-time compare: uses `hmac.compare_digest`.
    """

    secret_key = str(secret or "").encode("utf-8")
    if not secret_key:
        return False
    provided = str(signature or "").strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    if not provided:
        return False
    expected = hmac.new(secret_key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


def _parse_paid_at(value: Any) -> dt.datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _plan_duration_days(plan_code: str) -> int:
    """
    Derive subscription duration from plan code (monthly/quarterly/yearly).

    We keep duration out of the webhook payload to prevent tampering.
    """

    code = str(plan_code or "").strip().lower()
    if code.endswith(("_yearly", "-yearly", ":yearly")) or "yearly" in code:
        return 365
    if code.endswith(("_quarterly", "-quarterly", ":quarterly")) or "quarterly" in code or "quarter" in code:
        return 90
    if code.endswith(("_monthly", "-monthly", ":monthly")) or "monthly" in code:
        return 30
    # Default to monthly for unknown plan codes.
    return 30


def _parse_int_field(value: Any, *, default: int, name: str) -> int:
    if value is None or value == "":
        return int(default)
    # bool is a subclass of int, treat it as invalid for webhook payloads.
    if isinstance(value, bool):
        raise PaymentWebhookError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise PaymentWebhookError(f"{name} must be an integer")
        return int(value)
    raw = str(value).strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception as exc:  # noqa: BLE001
        raise PaymentWebhookError(f"{name} must be an integer") from exc


def process_payment_webhook(
    repo: BillingRepository,
    event: dict[str, Any],
    *,
    require_existing_order: bool = True,
) -> dict[str, Any]:
    """
    Process a normalized payment webhook event.

    Security / integrity properties:
    - Webhook must not create orders (order-centric processing).
    - Points are granted idempotently per order, not per event_id.
    - Subscription duration is derived from plan code, not from webhook payload.
    """

    event_type = str(event.get("event_type") or event.get("type") or "").strip().lower()
    log_event(
        _LOGGER,
        20,
        "billing.webhook.received",
        event_type=event_type or "unknown",
        event_id=str(event.get("event_id") or event.get("id") or ""),
    )
    if event_type in TIMEOUT_EVENT_TYPES:
        return _process_timeout(repo, event)
    if event_type in REFUND_EVENT_TYPES:
        return _process_refund(repo, event)
    if event_type != EVENT_TYPE_PAYMENT_SUCCEEDED:
        log_event(_LOGGER, 20, "billing.webhook.ignored", event_type=event_type or "unknown")
        return {"status": "ignored", "reason": f"unsupported event_type={event_type or '-'}"}

    event_id = str(event.get("event_id") or event.get("id") or event.get("idempotency_key") or "").strip()
    if not event_id:
        raise PaymentWebhookError("event_id is required")

    data = _extract_data(event)
    external_order_id = str(data.get("external_order_id") or data.get("order_id") or "").strip()
    if not external_order_id:
        raise PaymentWebhookError("external_order_id is required")

    order = repo.get_order_by_external_order_id(external_order_id)
    if not order:
        if require_existing_order:
            raise PaymentWebhookError(f"order not found: {external_order_id}")
        raise PaymentWebhookError("order creation from webhook is disabled")

    # Load plan via relationship; validate optional payload hints against DB truth.
    plan = getattr(order, "plan", None) or repo.get_plan_by_id(order.plan_id)
    if not plan:
        raise PaymentWebhookError(f"plan not found for order: {order.id}")

    payload_user_id = str(data.get("user_id") or "").strip() or None
    if payload_user_id and payload_user_id != order.user_id:
        raise PaymentWebhookError("user_id mismatch for external_order_id")

    payload_plan_code = str(data.get("plan_code") or "").strip() or None
    if payload_plan_code and payload_plan_code != plan.code:
        raise PaymentWebhookError("plan_code mismatch for external_order_id")

    if data.get("amount_cents") is not None:
        amount_cents = _parse_int_field(data.get("amount_cents"), default=int(order.amount_cents), name="amount_cents")
        if int(amount_cents) != int(order.amount_cents):
            raise PaymentWebhookError("amount_cents mismatch for external_order_id")

    if data.get("currency") is not None:
        currency = str(data.get("currency") or "").strip().lower()
        if currency and currency != str(order.currency or "").strip().lower():
            raise PaymentWebhookError("currency mismatch for external_order_id")

    paid_at = _parse_paid_at(data.get("paid_at"))
    current = paid_at or dt.datetime.now(dt.timezone.utc)
    duration_days = _plan_duration_days(getattr(plan, "code", ""))
    grant_points = int(getattr(plan, "monthly_points", 0) or 0)
    # v1 commercial rule:
    # - renew/cover always resets subscription window from "now"
    # - grant full plan credits every successful paid order
    active_sub = repo.get_active_subscription(order.user_id, now=current)
    reset_existing_active = active_sub is not None
    upgrade_applied = bool(
        active_sub is not None and str(getattr(active_sub, "plan_id", "") or "") != str(getattr(plan, "id", "") or "")
    )

    try:
        repo.mark_order_paid(order.id, paid_at=paid_at, provider_payload=json.dumps(event, ensure_ascii=False))
        subscription = repo.activate_subscription_from_order(
            order.id,
            duration_days=duration_days,
            reset_existing_active=reset_existing_active,
            now=current,
        )
    except BillingStateError as exc:
        raise PaymentWebhookError(str(exc)) from exc

    point_flow_id = None
    if grant_points:
        point_flow = repo.record_point_flow(
            # Always credit against the order owner (not the webhook payload).
            user_id=order.user_id,
            flow_type=PointFlowType.GRANT,
            points=grant_points,
            subscription_id=subscription.id,
            order_id=order.id,
            # Idempotency must be tied to the order (WeChat/Stripe may resend with a new event_id).
            idempotency_key=f"{POINTS_GRANT_IDEMPOTENCY_PREFIX}{order.id}",
            note=f"webhook {event_id}",
        )
        point_flow_id = point_flow.id

    result = {
        "status": "processed",
        "event_id": event_id,
        "order_id": order.id,
        "subscription_id": subscription.id,
        "point_flow_id": point_flow_id,
        "points_granted": int(grant_points),
        "upgrade_applied": bool(upgrade_applied),
    }
    log_event(
        _LOGGER,
        20,
        "billing.webhook.processed",
        event_id=event_id,
        order_id=order.id,
        subscription_id=subscription.id,
    )
    return result


def _extract_event_id(event: dict[str, Any]) -> str:
    return str(event.get("event_id") or event.get("id") or event.get("idempotency_key") or "").strip()


def _extract_data(event: dict[str, Any]) -> dict[str, Any]:
    raw_data = event.get("data")
    if isinstance(raw_data, dict):
        return dict(raw_data)
    return dict(event)


def _process_timeout(repo: BillingRepository, event: dict[str, Any]) -> dict[str, Any]:
    event_id = _extract_event_id(event)
    if not event_id:
        raise PaymentWebhookError("event_id is required")
    data = _extract_data(event)

    external_order_id = str(data.get("external_order_id") or data.get("order_id") or "").strip()
    if not external_order_id:
        raise PaymentWebhookError("external_order_id is required for timeout")

    order = repo.get_order_by_external_order_id(external_order_id)
    if not order:
        raise PaymentWebhookError(f"order not found: {external_order_id}")

    if order.status == OrderStatus.PAID:
        return {"status": "ignored", "reason": "order already paid", "event_id": event_id, "order_id": order.id}
    if order.status == OrderStatus.CANCELED:
        return {"status": "timeout_closed", "reason": "already closed", "event_id": event_id, "order_id": order.id}
    try:
        repo.mark_order_canceled(order.id, now=dt.datetime.now(dt.timezone.utc), provider_payload=json.dumps(event, ensure_ascii=False))
    except BillingStateError as exc:
        raise PaymentWebhookError(str(exc)) from exc
    return {"status": "timeout_closed", "event_id": event_id, "order_id": order.id}


def _process_refund(repo: BillingRepository, event: dict[str, Any]) -> dict[str, Any]:
    event_id = _extract_event_id(event)
    if not event_id:
        raise PaymentWebhookError("event_id is required")
    data = _extract_data(event)

    external_order_id = str(data.get("external_order_id") or data.get("order_id") or "").strip()
    if not external_order_id:
        raise PaymentWebhookError("external_order_id is required for refund")

    order = repo.get_order_by_external_order_id(external_order_id)
    if not order:
        raise PaymentWebhookError(f"order not found: {external_order_id}")

    if order.status == OrderStatus.REFUNDED:
        return {"status": "refunded", "reason": "already_refunded", "event_id": event_id, "order_id": order.id}

    try:
        repo.mark_order_refunded(order.id, now=dt.datetime.now(dt.timezone.utc), provider_payload=json.dumps(event, ensure_ascii=False))
    except BillingStateError as exc:
        raise PaymentWebhookError(str(exc)) from exc

    grant_points = repo.get_order_grant_points(order.id)
    point_flow_id = None
    if grant_points:
        refund_flow = repo.record_point_flow(
            user_id=order.user_id,
            flow_type=PointFlowType.REFUND,
            points=-abs(int(grant_points)),
            order_id=order.id,
            idempotency_key=f"{POINTS_REFUND_IDEMPOTENCY_PREFIX}{order.id}",
            note=f"refund webhook {event_id}",
        )
        point_flow_id = refund_flow.id

    return {"status": "refunded", "event_id": event_id, "order_id": order.id, "point_flow_id": point_flow_id}


def close_timed_out_orders(
    repo: BillingRepository,
    *,
    timeout_seconds: int,
    now: dt.datetime | None = None,
) -> int:
    """
    Close stale pending orders.

    This is meant for scheduled jobs / admin tools (not the payment webhook).
    """

    current = now or dt.datetime.now(dt.timezone.utc)
    cutoff = current - dt.timedelta(seconds=int(timeout_seconds))

    # Fetch in Python and compare with UTC aware datetimes to avoid SQLite timezone quirks.
    pending = repo.list_orders(limit=200, offset=0, status=OrderStatus.PENDING)
    closed = 0
    for order in pending:
        created_at = getattr(order, "created_at", None)
        if not created_at:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        if created_at <= cutoff:
            try:
                repo.mark_order_canceled(order.id, now=current)
                closed += 1
            except BillingStateError:
                continue
    return closed
