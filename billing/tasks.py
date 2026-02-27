"""Celery tasks for billing maintenance jobs."""

from __future__ import annotations

import logging

from observability import get_logger, log_event

_LOGGER = get_logger("antihub.billing.tasks")

# Default timeout for stale pending orders: 1 hour.
ORDER_TIMEOUT_SECONDS: int = 3600


def run_close_timed_out_orders() -> int:
    """Close stale pending orders. Intended for periodic scheduling via Celery beat."""
    from .db import session_scope
    from .repository import BillingRepository
    from .service import close_timed_out_orders

    with session_scope() as session:
        repo = BillingRepository(session)
        closed = close_timed_out_orders(repo, timeout_seconds=ORDER_TIMEOUT_SECONDS)

    if closed:
        log_event(
            _LOGGER,
            logging.INFO,
            "billing.close_timed_out_orders.completed",
            closed_count=closed,
            timeout_seconds=ORDER_TIMEOUT_SECONDS,
        )
    return closed
