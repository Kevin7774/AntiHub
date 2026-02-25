#!/usr/bin/env python3
"""
Billing Chaos & Resilience Suite (AntiHub V2.0)

Goals:
- Flood webhook with malformed payloads and invalid signatures (should not 5xx).
- Replay attacks: same valid payload concurrently (must be idempotent).
- Data integrity: revenue == sum(paid orders) and replay does not double-credit points.
- Self-healing: /health/billing verifies DB connectivity + config readiness.

This is a *tool* only; it does not modify business code.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import string
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import httpx

# Make project modules importable when script is run from tools/.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from billing import BillingRepository, session_scope  # noqa: E402
from billing.models import Order, OrderStatus, PointFlow, PointFlowType  # noqa: E402


def getenv_required(name: str) -> str:
    value = str(os.getenv(name, "")).strip()
    if not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def _rand_id(prefix: str, length: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return f"{prefix}_{''.join(random.choice(alphabet) for _ in range(length))}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AntiHub billing chaos & resilience suite")
    parser.add_argument(
        "--webhook-url",
        default=os.getenv("PAYMENT_WEBHOOK_URL", "http://127.0.0.1:8010/billing/webhooks/payment"),
        help="Target billing webhook URL",
    )
    parser.add_argument(
        "--health-url",
        default=os.getenv("BILLING_HEALTH_URL", "http://127.0.0.1:8010/health/billing"),
        help="Target billing health URL",
    )
    parser.add_argument(
        "--plan-code",
        default=os.getenv("CHAOS_PLAN_CODE", "chaos_monthly"),
        help="Plan code to use in generated payloads",
    )
    parser.add_argument(
        "--amount-cents",
        type=int,
        default=int(os.getenv("CHAOS_AMOUNT_CENTS", "9900")),
        help="Payment amount for happy-path cases (cents)",
    )
    parser.add_argument(
        "--grant-points",
        type=int,
        default=int(os.getenv("CHAOS_GRANT_POINTS", "1000")),
        help="Points to grant for happy-path cases",
    )
    parser.add_argument(
        "--attack-count",
        type=int,
        default=int(os.getenv("CHAOS_ATTACK_COUNT", "50")),
        help="Number of malformed/invalid-signature requests",
    )
    parser.add_argument(
        "--replay-concurrency",
        type=int,
        default=int(os.getenv("CHAOS_REPLAY_CONCURRENCY", "10")),
        help="Replay attack concurrency per scenario",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=float(os.getenv("CHAOS_HTTP_TIMEOUT", "20")),
        help="HTTP timeout seconds",
    )
    return parser.parse_args()


def ensure_plan(plan_code: str, amount_cents: int, grant_points: int) -> None:
    with session_scope() as session:
        repo = BillingRepository(session)
        existing = repo.get_plan_by_code(plan_code)
        if existing:
            repo.update_plan(
                existing.id,
                name="Commercial Monthly",
                price_cents=int(amount_cents),
                monthly_points=int(grant_points),
                currency="cny",
                description="seeded by chaos_suite.py",
                active=True,
            )
            return
        repo.create_plan(
            code=plan_code,
            name="Commercial Monthly",
            price_cents=int(amount_cents),
            monthly_points=int(grant_points),
            currency="cny",
            description="seeded by chaos_suite.py",
            active=True,
        )


def snapshot_financials() -> Dict[str, int]:
    with session_scope() as session:
        paid_count = int(session.scalar(select(func.count()).select_from(Order).where(Order.status == OrderStatus.PAID)) or 0)
        paid_sum = int(
            session.scalar(
                select(func.coalesce(func.sum(Order.amount_cents), 0)).where(Order.status == OrderStatus.PAID)
            )
            or 0
        )
        grant_count = int(
            session.scalar(
                select(func.count()).select_from(PointFlow).where(PointFlow.flow_type == PointFlowType.GRANT)
            )
            or 0
        )
        grant_sum = int(
            session.scalar(
                select(func.coalesce(func.sum(PointFlow.points), 0)).where(PointFlow.flow_type == PointFlowType.GRANT)
            )
            or 0
        )
    return {"paid_count": paid_count, "paid_sum": paid_sum, "grant_count": grant_count, "grant_sum": grant_sum}


@dataclass(frozen=True)
class AttackCase:
    name: str
    body: bytes
    signature_mode: str  # valid|invalid|missing
    expect_4xx: bool = True


def build_attack_cases(plan_code: str, amount_cents: int, grant_points: int, count: int) -> List[AttackCase]:
    cases: List[AttackCase] = []
    for idx in range(max(1, int(count))):
        kind = idx % 6
        if kind == 0:
            cases.append(AttackCase(name="invalid_json", body=b"{not_json", signature_mode="valid"))
        elif kind == 1:
            payload = {"event_type": "payment.succeeded", "event_id": _rand_id("evt"), "data": {"plan_code": plan_code}}
            cases.append(AttackCase(name="missing_user_id", body=json.dumps(payload).encode("utf-8"), signature_mode="valid"))
        elif kind == 2:
            payload = {
                "event_type": "payment.succeeded",
                "event_id": _rand_id("evt"),
                "data": {
                    "user_id": "alice",
                    "plan_code": plan_code,
                    "external_order_id": _rand_id("ord"),
                    "amount_cents": "100.00",
                    "currency": "cny",
                    "grant_points": grant_points,
                    "duration_days": 30,
                },
            }
            cases.append(AttackCase(name="bad_amount_format", body=json.dumps(payload).encode("utf-8"), signature_mode="valid"))
        elif kind == 3:
            payload = {
                "event_type": "payment.succeeded",
                "event_id": _rand_id("evt"),
                "data": {
                    "user_id": "alice",
                    "plan_code": plan_code,
                    "external_order_id": _rand_id("ord"),
                    "amount_cents": amount_cents,
                    "currency": "cny",
                    "grant_points": grant_points,
                    "duration_days": 30,
                },
            }
            cases.append(AttackCase(name="invalid_signature", body=json.dumps(payload).encode("utf-8"), signature_mode="invalid"))
        elif kind == 4:
            payload = {
                "event_type": "payment.succeeded",
                "event_id": _rand_id("evt"),
                "data": {
                    "user_id": "alice",
                    "plan_code": plan_code,
                    "external_order_id": _rand_id("ord"),
                    "amount_cents": amount_cents,
                    "currency": "cny",
                    "grant_points": grant_points,
                    "duration_days": 30,
                },
            }
            cases.append(AttackCase(name="missing_signature", body=json.dumps(payload).encode("utf-8"), signature_mode="missing"))
        else:
            payload = {"event_type": "unknown.event", "event_id": _rand_id("evt"), "data": {"user_id": "alice"}}
            cases.append(AttackCase(name="unsupported_event_type", body=json.dumps(payload).encode("utf-8"), signature_mode="valid"))
    return cases


async def send_case(
    client: httpx.AsyncClient,
    url: str,
    secret: str,
    case: AttackCase,
    timeout: float,
) -> Dict[str, Any]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if case.signature_mode == "valid":
        headers["X-Signature"] = sign_payload(case.body, secret)
    elif case.signature_mode == "invalid":
        headers["X-Signature"] = "deadbeef" * 8
    # missing: no signature header

    start = time.perf_counter()
    try:
        resp = await client.post(url, content=case.body, headers=headers, timeout=timeout)
        return {
            "name": case.name,
            "status": int(resp.status_code),
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "body": (resp.text or "").strip()[:200],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": case.name,
            "status": 0,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "body": f"request_error: {exc}",
        }


async def run_attack(url: str, secret: str, cases: List[AttackCase], timeout: float, concurrency: int) -> List[Dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with httpx.AsyncClient(trust_env=False) as client:

        async def wrapped(case: AttackCase):
            async with semaphore:
                return await send_case(client, url, secret, case, timeout)

        return await asyncio.gather(*[wrapped(c) for c in cases])


async def replay_attack(
    url: str,
    secret: str,
    payloads: List[bytes],
    *,
    timeout: float,
) -> List[int]:
    async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
        tasks = []
        for body in payloads:
            headers = {"Content-Type": "application/json", "X-Signature": sign_payload(body, secret)}
            tasks.append(client.post(url, content=body, headers=headers))
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        status_codes: List[int] = []
        for item in responses:
            if isinstance(item, Exception):
                status_codes.append(0)
            else:
                status_codes.append(int(item.status_code))
        return status_codes


def main() -> int:
    args = parse_args()
    secret = getenv_required("PAYMENT_WEBHOOK_SECRET")

    # Ensure plan exists so webhook processing can resolve plan_code.
    ensure_plan(args.plan_code, args.amount_cents, args.grant_points)

    print(f"[suite] health_check url={args.health_url}")
    with httpx.Client(trust_env=False, timeout=args.http_timeout) as client:
        resp = client.get(args.health_url)
        if resp.status_code != 200:
            raise RuntimeError(f"/health/billing failed: status={resp.status_code} body={resp.text[:200]}")
        data = resp.json()
        if str(data.get("status")) != "ok":
            raise RuntimeError(f"/health/billing not ok: {data}")

    before = snapshot_financials()
    print(f"[suite] snapshot_before {before}")

    # 1) Attack simulation (malformed + invalid signature) should never produce 5xx.
    attack_cases = build_attack_cases(args.plan_code, args.amount_cents, args.grant_points, args.attack_count)
    print(f"[suite] attack_simulation cases={len(attack_cases)} url={args.webhook_url}")
    attack_results = asyncio.run(
        run_attack(
            url=args.webhook_url,
            secret=secret,
            cases=attack_cases,
            timeout=args.http_timeout,
            concurrency=min(50, max(1, len(attack_cases))),
        )
    )
    server_5xx = sum(1 for r in attack_results if int(r["status"]) >= 500)
    request_err = sum(1 for r in attack_results if int(r["status"]) == 0)
    blocked_403 = sum(1 for r in attack_results if int(r["status"]) == 403)
    rejected_400 = sum(1 for r in attack_results if int(r["status"]) == 400)
    ok_2xx = sum(1 for r in attack_results if 200 <= int(r["status"]) < 300)
    print(
        f"[suite] attack_summary 2xx={ok_2xx} 400={rejected_400} 403={blocked_403} 5xx={server_5xx} err={request_err}"
    )
    if server_5xx:
        samples = [r for r in attack_results if int(r["status"]) >= 500][:3]
        raise RuntimeError(f"attack simulation produced 5xx: samples={samples}")
    if request_err:
        samples = [r for r in attack_results if int(r["status"]) == 0][:3]
        raise RuntimeError(f"attack simulation request errors: samples={samples}")

    # 2) Replay attacks (idempotency):
    user_id = _rand_id("user")
    external_order_id = _rand_id("ord_ext")
    with session_scope() as session:
        repo = BillingRepository(session)
        plan = repo.get_plan_by_code(args.plan_code)
        if not plan:
            raise RuntimeError(f"plan not found: {args.plan_code}")
        # Webhook processing must never create orders; seed a pending order first.
        repo.create_order(
            user_id=user_id,
            plan_id=plan.id,
            amount_cents=int(args.amount_cents),
            currency="cny",
            provider="chaos-suite",
            external_order_id=external_order_id,
            idempotency_key=f"chaos-seed:{external_order_id}",
        )
    base_event = {
        "event_type": "payment.succeeded",
        "event_id": _rand_id("evt"),
        "provider": "chaos-suite",
        "data": {
            "user_id": user_id,
            "plan_code": args.plan_code,
            "external_order_id": external_order_id,
            "amount_cents": args.amount_cents,
            "currency": "cny",
            "duration_days": 30,
            "grant_points": args.grant_points,
        },
    }
    body_same = json.dumps(base_event, ensure_ascii=False).encode("utf-8")
    print(f"[suite] replay_attack same_payload concurrency={args.replay_concurrency}")
    statuses = asyncio.run(
        replay_attack(
            args.webhook_url,
            secret,
            payloads=[body_same for _ in range(max(1, args.replay_concurrency))],
            timeout=args.http_timeout,
        )
    )
    if any(code >= 500 or code == 0 for code in statuses):
        raise RuntimeError(f"replay attack produced errors: {statuses}")
    if not all(200 <= code < 300 for code in statuses):
        raise RuntimeError(f"replay attack not all 2xx: {statuses}")

    # WeChat-style replay: same order, different event_id.
    print(f"[suite] replay_attack same_order_new_event_ids concurrency={args.replay_concurrency}")
    bodies: List[bytes] = []
    for _ in range(max(1, args.replay_concurrency)):
        ev = dict(base_event)
        ev["event_id"] = _rand_id("evt")
        bodies.append(json.dumps(ev, ensure_ascii=False).encode("utf-8"))
    statuses2 = asyncio.run(replay_attack(args.webhook_url, secret, payloads=bodies, timeout=args.http_timeout))
    if any(code >= 500 or code == 0 for code in statuses2):
        raise RuntimeError(f"replay attack (new event_ids) produced errors: {statuses2}")
    if not all(200 <= code < 300 for code in statuses2):
        raise RuntimeError(f"replay attack (new event_ids) not all 2xx: {statuses2}")

    after = snapshot_financials()
    print(f"[suite] snapshot_after {after}")

    # 3) Data integrity checks:
    paid_count_delta = after["paid_count"] - before["paid_count"]
    paid_sum_delta = after["paid_sum"] - before["paid_sum"]
    grant_count_delta = after["grant_count"] - before["grant_count"]
    grant_sum_delta = after["grant_sum"] - before["grant_sum"]

    # Exactly 1 successful paid order and 1 grant flow should be created across all replays.
    if paid_count_delta != 1:
        raise RuntimeError(f"paid_count_delta expected 1, got {paid_count_delta}")
    if paid_sum_delta != int(args.amount_cents):
        raise RuntimeError(f"paid_sum_delta expected {args.amount_cents}, got {paid_sum_delta}")
    if grant_count_delta != 1:
        raise RuntimeError(f"grant_count_delta expected 1, got {grant_count_delta}")
    if grant_sum_delta != int(args.grant_points):
        raise RuntimeError(f"grant_sum_delta expected {args.grant_points}, got {grant_sum_delta}")

    with session_scope() as session:
        repo = BillingRepository(session)
        revenue = repo.get_total_revenue_cents()
        sum_paid = int(
            session.scalar(
                select(func.coalesce(func.sum(Order.amount_cents), 0)).where(Order.status == OrderStatus.PAID)
            )
            or 0
        )
        if revenue != sum_paid:
            raise RuntimeError(f"revenue mismatch: repo={revenue} sum_paid={sum_paid}")

    print("[suite] ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
