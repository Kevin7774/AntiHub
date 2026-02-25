#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal

import httpx

# Make project modules importable when script is run from tools/.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from billing import BillingRepository, session_scope  # noqa: E402

SignatureMode = Literal["valid", "invalid", "missing"]


@dataclass
class ChaosCase:
    case_id: str
    category: str
    signature_mode: SignatureMode
    payload: Dict[str, Any]
    note: str = ""


def getenv_required(name: str) -> str:
    value = str(os.getenv(name, "")).strip()
    if not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chaos test for /billing/webhooks/payment")
    parser.add_argument(
        "--webhook-url",
        default=os.getenv("PAYMENT_WEBHOOK_URL", "http://127.0.0.1:8010/billing/webhooks/payment"),
        help="Target webhook URL",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=int(os.getenv("CHAOS_CASE_COUNT", "50")),
        help="Number of chaos payloads",
    )
    parser.add_argument(
        "--minimax-base-url",
        default=os.getenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1"),
        help="MiniMax base URL",
    )
    parser.add_argument(
        "--minimax-model",
        default=os.getenv("MINIMAX_MODEL", "abab6.5s-chat"),
        help="MiniMax chat model",
    )
    parser.add_argument(
        "--plan-code",
        default=os.getenv("CHAOS_PLAN_CODE", "pro_chaos"),
        help="Plan code used in generated payment payloads",
    )
    parser.add_argument(
        "--ensure-plan",
        action="store_true",
        default=True,
        help="Ensure billing plan exists locally before sending webhook requests",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("CHAOS_CONCURRENCY", "8")),
        help="Concurrent requests",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("CHAOS_HTTP_TIMEOUT", "15")),
        help="HTTP timeout seconds",
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=None,
        help="MiniMax HTTP timeout seconds (defaults to --timeout)",
    )
    parser.add_argument(
        "--llm-trust-env",
        action=argparse.BooleanOptionalAction,
        default=str(os.getenv("CHAOS_LLM_TRUST_ENV", "true")).strip().lower() in {"1", "true", "yes"},
        help="Allow MiniMax calls to use proxy env vars (HTTP_PROXY/HTTPS_PROXY).",
    )
    parser.add_argument(
        "--report-json",
        default=os.getenv("CHAOS_REPORT_PATH", ""),
        help="Optional path to write a JSON report",
    )
    return parser.parse_args()


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def ensure_plan(plan_code: str) -> None:
    # Reuse repository transaction layer; do not touch business endpoint code.
    with session_scope() as session:
        repo = BillingRepository(session)
        existing = repo.get_plan_by_code(plan_code)
        if existing:
            repo.update_plan(
                existing.id,
                name="Chaos Test Plan",
                price_cents=9900,
                monthly_points=1000,
                currency="usd",
                description="seeded by chaos_payment_test.py",
                active=True,
            )
            return
        repo.create_plan(
            code=plan_code,
            name="Chaos Test Plan",
            price_cents=9900,
            monthly_points=1000,
            currency="usd",
            description="seeded by chaos_payment_test.py",
            active=True,
        )


def seed_orders_for_cases(cases: List[ChaosCase], default_plan_code: str) -> None:
    """
    Webhook processing must be idempotent and MUST NOT create orders.

    Seed pending orders for cases that look like valid payment callbacks so we
    can exercise the real processing path (paid -> subscription -> points).
    """

    with session_scope() as session:
        repo = BillingRepository(session)
        default_plan = repo.get_plan_by_code(default_plan_code)
        for case in cases:
            if case.category not in {"happy_path", "duplicate_order", "bad_amount_format"}:
                continue
            payload = case.payload if isinstance(case.payload, dict) else {}
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            external_order_id = str(data.get("external_order_id") or "").strip()
            user_id = str(data.get("user_id") or "").strip()
            plan_code = str(data.get("plan_code") or default_plan_code).strip()
            if not external_order_id or not user_id:
                continue

            plan = repo.get_plan_by_code(plan_code) or default_plan
            if not plan:
                continue

            repo.create_order(
                user_id=user_id,
                plan_id=plan.id,
                amount_cents=int(plan.price_cents),
                currency=str(plan.currency or "usd"),
                provider="chaos-seed",
                external_order_id=external_order_id,
                idempotency_key=f"seed:{user_id}:{external_order_id}",
            )


def _extract_json_array(text: str) -> List[Dict[str, Any]]:
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("empty llm response")
    # Accept markdown fenced JSON or plain JSON.
    fenced = re.search(r"```(?:json)?\s*(\[.*\])\s*```", stripped, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else stripped
    # Best-effort bracket extraction if model adds leading prose.
    if not candidate.lstrip().startswith("["):
        left = candidate.find("[")
        right = candidate.rfind("]")
        if left >= 0 and right > left:
            candidate = candidate[left : right + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, list):
        raise ValueError("llm output is not a list")
    normalized: List[Dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            normalized.append(item)
    return normalized


def call_minimax_for_cases(
    api_key: str,
    base_url: str,
    model: str,
    count: int,
    plan_code: str,
    timeout: float,
    trust_env: bool,
) -> List[ChaosCase]:
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Large generations are prone to truncation/malformed JSON. Batch it.
    batch_size = int(os.getenv("CHAOS_LLM_BATCH", "25"))
    max_tokens = int(os.getenv("CHAOS_LLM_MAX_TOKENS", "6000"))

    def call_once(batch_count: int, *, temperature: float, batch_idx: int) -> List[Dict[str, Any]]:
        system_prompt = (
            "You are a faulty payment gateway simulator. "
            "Return STRICT JSON only: a JSON array. No markdown. No prose. "
            "Use double quotes. No trailing commas. The last character must be ']'."
        )
        user_prompt = (
            f"Generate exactly {batch_count} webhook test cases for payment callback chaos testing.\n"
            "Output MUST be valid JSON.\n"
            "Schema for each item:\n"
            "{\n"
            "  \"case_id\": \"string\",\n"
            "  \"category\": \"happy_path|bad_signature|missing_fields|bad_amount_format|duplicate_order\",\n"
            "  \"signature_mode\": \"valid|invalid|missing\",\n"
            "  \"note\": \"string\",\n"
            "  \"payload\": {\n"
            "    \"event_type\": \"payment.succeeded\",\n"
            "    \"event_id\": \"string\",\n"
            "    \"provider\": \"chaos-gateway\",\n"
            "    \"data\": {\n"
            "      \"user_id\": \"string\",\n"
            f"      \"plan_code\": \"{plan_code}\",\n"
            "      \"external_order_id\": \"string\",\n"
            "      \"amount_cents\": 9900,\n"
            "      \"currency\": \"usd\",\n"
            "      \"duration_days\": 30\n"
            "    }\n"
            "  }\n"
            "}\n"
            "Constraints:\n"
            "- Include all 5 categories, balanced as much as possible.\n"
            "- duplicate_order cases must contain repeated external_order_id and repeated event_id.\n"
            "- bad_signature cases should still have logically valid payload.\n"
            "- missing_fields cases should remove required fields (e.g. event_id/user_id/plan_code).\n"
            "- bad_amount_format cases should include incorrect amount_cents types/values.\n"
            "- happy_path cases should be valid business payloads.\n"
            "- Keep note short; do not include unescaped newlines.\n"
            f"Batch index: {batch_idx}\n"
            "Return JSON array only."
        )
        payload = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.post(endpoint, headers=headers, json=payload, timeout=timeout, trust_env=trust_env)
            except httpx.RequestError as exc:
                # Includes timeouts, DNS errors, etc. Retry with backoff.
                last_error = exc
                time.sleep(0.75 * (attempt + 1))
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = int(exc.response.status_code)
                detail = (exc.response.text or "").strip().replace("\n", " ")[:600]
                # Retry transient upstream errors; fail fast on auth/quota and other 4xx.
                if status >= 500:
                    last_error = RuntimeError(f"minimax http {status}: {detail}")
                    time.sleep(0.75 * (attempt + 1))
                    continue
                raise RuntimeError(f"minimax http {status}: {detail}") from exc

            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                # Sometimes the upstream returns an empty/partial body; retry.
                time.sleep(0.5 * (attempt + 1))
                continue

            choices = data.get("choices") or []
            if not choices:
                last_error = RuntimeError("minimax response missing choices")
                time.sleep(0.5 * (attempt + 1))
                continue

            message = choices[0].get("message") or {}
            content = message.get("content")
            if not content:
                last_error = RuntimeError("minimax response missing content")
                time.sleep(0.5 * (attempt + 1))
                continue

            try:
                return _extract_json_array(str(content))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
                continue

        raise RuntimeError(f"minimax response parse failed after retries: {last_error}") from last_error

    out: List[ChaosCase] = []
    remaining = count
    batch_idx = 0
    while remaining > 0:
        batch_count = min(max(1, batch_size), remaining)
        # Retry once with lower temperature if JSON parsing fails.
        last_exc: Exception | None = None
        for temperature in (0.9, 0.2):
            try:
                raw_items = call_once(batch_count, temperature=temperature, batch_idx=batch_idx)
                out.extend(normalize_cases(raw_items, count=batch_count, plan_code=plan_code))
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        remaining -= batch_count
        batch_idx += 1
    return out[:count]


def deterministic_cases(count: int, plan_code: str) -> List[ChaosCase]:
    # Deterministic fallback used when LLM is unavailable or returns malformed JSON.
    buckets = ["happy_path", "bad_signature", "missing_fields", "bad_amount_format", "duplicate_order"]
    per_bucket = max(1, count // len(buckets))
    out: List[ChaosCase] = []
    run_nonce = f"{random.randint(0, 16**6 - 1):06x}"

    def make_base(idx: int) -> Dict[str, Any]:
        return {
            "event_type": "payment.succeeded",
            "event_id": f"evt_{run_nonce}_{idx:03d}",
            "provider": "chaos-gateway",
            "data": {
                "user_id": f"user_{idx % 7}",
                "plan_code": plan_code,
                "external_order_id": f"ord_{run_nonce}_{idx:04d}",
                "amount_cents": 9900,
                "currency": "usd",
                "duration_days": 30,
            },
        }

    index = 0
    for category in buckets:
        for i in range(per_bucket):
            payload = make_base(index)
            signature_mode: SignatureMode = "valid"
            note = ""

            if category == "bad_signature":
                signature_mode = "invalid"
                note = "valid payload with intentionally invalid signature"
            elif category == "missing_fields":
                removed = random.choice(["event_id", "user_id", "plan_code", "external_order_id"])
                if removed == "event_id":
                    payload.pop("event_id", None)
                else:
                    payload["data"].pop(removed, None)
                note = f"missing required field: {removed}"
            elif category == "bad_amount_format":
                # NOTE: do not use None here; the webhook handler treats missing amount_cents as "skip validation".
                payload["data"]["amount_cents"] = random.choice(["100.00", "NaN", -1, {"raw": 100}, True, 100.5])
                note = "invalid amount_cents format"
            elif category == "duplicate_order":
                dup_group = i // 2
                payload["event_id"] = f"evt_dup_{run_nonce}_{dup_group:03d}"
                payload["data"]["external_order_id"] = f"ord_dup_{run_nonce}_{dup_group:03d}"
                # Ensure duplicate events share the same user_id, otherwise the webhook
                # handler will reject user_id mismatch for the same external_order_id.
                payload["data"]["user_id"] = f"user_dup_{dup_group % 7}"
                note = "fully duplicated order/event pair"
            else:
                note = "baseline valid case"

            out.append(
                ChaosCase(
                    case_id=f"fallback_{category}_{index:03d}",
                    category=category,
                    signature_mode=signature_mode,
                    payload=payload,
                    note=note,
                )
            )
            index += 1

    while len(out) < count:
        payload = make_base(index)
        out.append(
            ChaosCase(
                case_id=f"fallback_pad_{index:03d}",
                category="happy_path",
                signature_mode="valid",
                payload=payload,
                note="padding case",
            )
        )
        index += 1

    return out[:count]


def normalize_cases(raw_items: List[Dict[str, Any]], count: int, plan_code: str) -> List[ChaosCase]:
    normalized: List[ChaosCase] = []
    run_nonce = f"{random.randint(0, 16**6 - 1):06x}"
    for idx, raw in enumerate(raw_items):
        category = str(raw.get("category") or "happy_path").strip().lower()
        if category not in {"happy_path", "bad_signature", "missing_fields", "bad_amount_format", "duplicate_order"}:
            category = "happy_path"

        signature_mode = str(raw.get("signature_mode") or "valid").strip().lower()
        if signature_mode not in {"valid", "invalid", "missing"}:
            signature_mode = "valid"

        # Stabilize category intent regardless of LLM output.
        # - happy_path / duplicate_order should hit business logic => valid signature
        # - bad_signature should be blocked at the signature gate => invalid/missing
        # - missing_fields / bad_amount_format should be rejected by validation => valid signature
        if category in {"happy_path", "duplicate_order", "missing_fields", "bad_amount_format"}:
            signature_mode = "valid"
        elif category == "bad_signature":
            signature_mode = "missing" if (idx % 2) else "invalid"

        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

        def _norm_str(value: Any) -> str:
            return str(value or "").strip()

        # missing_fields must reliably fail validation (400), not be "ignored" (2xx).
        if category == "missing_fields":
            payload["event_type"] = "payment.succeeded"
            payload["provider"] = _norm_str(payload.get("provider")) or "chaos-gateway"
            if not isinstance(payload.get("data"), dict):
                payload["data"] = {}
            data = payload["data"]  # type: ignore[assignment]

            missing_targets = ["event_id", "external_order_id", "user_id", "plan_code"]
            removed = missing_targets[idx % len(missing_targets)]
            # Populate everything first, then remove one required field.
            payload["event_id"] = f"evt_miss_{run_nonce}_{idx:04d}"
            data["user_id"] = f"user_miss_{idx % 7}"
            data["plan_code"] = plan_code
            data["external_order_id"] = f"ord_miss_{run_nonce}_{idx:04d}"
            data["amount_cents"] = 9900
            data["currency"] = "usd"
            data["duration_days"] = 30

            if removed == "event_id":
                payload.pop("event_id", None)
            else:
                data.pop(removed, None)
        else:
            # Always drive the payment.succeeded path; other event types are out of scope for this tool.
            payload["event_type"] = "payment.succeeded"
            payload["provider"] = _norm_str(payload.get("provider")) or "chaos-gateway"

            event_id = _norm_str(payload.get("event_id"))
            if category == "duplicate_order":
                dup_group = idx // 2
                event_id = f"evt_dup_{run_nonce}_{dup_group:03d}"
            elif category == "happy_path":
                event_id = f"evt_hp_{run_nonce}_{idx:04d}"
            payload["event_id"] = event_id or f"evt_norm_{run_nonce}_{idx:03d}"

            user_id = _norm_str(data.get("user_id")) or f"user_{idx % 7}"
            # Always use the configured plan_code to avoid plan_code mismatch errors.
            plan_code_value = plan_code
            external_order_id = _norm_str(data.get("external_order_id"))
            if category == "duplicate_order":
                dup_group = idx // 2
                external_order_id = f"ord_dup_{run_nonce}_{dup_group:03d}"
                user_id = f"user_dup_{dup_group % 7}"
            elif category == "happy_path":
                # Don't allow LLM-provided ids to collide with previous runs.
                external_order_id = f"ord_hp_{run_nonce}_{idx:04d}"
                user_id = f"user_hp_{idx % 7}"
            elif category == "bad_amount_format":
                external_order_id = f"ord_amt_{run_nonce}_{idx:04d}"
                user_id = f"user_amt_{idx % 7}"
            external_order_id = external_order_id or f"ord_norm_{run_nonce}_{idx:04d}"

            data["user_id"] = user_id
            data["plan_code"] = plan_code_value
            data["external_order_id"] = external_order_id

            if category != "bad_amount_format":
                # Keep deterministic amount/currency so seeded orders always match.
                data["amount_cents"] = 9900
                data["currency"] = "usd"
            else:
                # Keep amount malformed but ensure other fields exist.
                bad_values = ["100.00", "NaN", {"raw": 100}, True, 100.5, -1]
                data["amount_cents"] = bad_values[idx % len(bad_values)]
                data["currency"] = "usd"

            if data.get("duration_days") in {None, ""}:
                data["duration_days"] = 30

        payload["data"] = data

        normalized.append(
            ChaosCase(
                case_id=str(raw.get("case_id") or f"llm_case_{idx:03d}"),
                category=category,
                signature_mode=signature_mode,  # type: ignore[arg-type]
                payload=payload,
                note=str(raw.get("note") or ""),
            )
        )

    if len(normalized) < count:
        normalized.extend(deterministic_cases(count - len(normalized), plan_code))

    return normalized[:count]


async def send_case(
    client: httpx.AsyncClient,
    webhook_url: str,
    webhook_secret: str,
    case: ChaosCase,
) -> Dict[str, Any]:
    body = json.dumps(case.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers: Dict[str, str] = {"Content-Type": "application/json"}

    if case.signature_mode == "valid":
        headers["X-Signature"] = sign_payload(body, webhook_secret)
    elif case.signature_mode == "invalid":
        headers["X-Signature"] = "deadbeef" * 8

    start = time.perf_counter()
    try:
        response = await client.post(webhook_url, content=body, headers=headers)
        latency_ms = int((time.perf_counter() - start) * 1000)
        snippet = response.text.strip().replace("\n", " ")[:220]
        return {
            "case_id": case.case_id,
            "category": case.category,
            "signature_mode": case.signature_mode,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "response": snippet,
        }
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "case_id": case.case_id,
            "category": case.category,
            "signature_mode": case.signature_mode,
            "status_code": 0,
            "latency_ms": latency_ms,
            "response": f"request_error: {exc}",
        }


async def run_attack(cases: List[ChaosCase], webhook_url: str, webhook_secret: str, timeout: float, concurrency: int):
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        async def wrapped(case: ChaosCase):
            async with semaphore:
                return await send_case(client, webhook_url, webhook_secret, case)

        tasks = [wrapped(case) for case in cases]
        return await asyncio.gather(*tasks)


def print_summary(results: List[Dict[str, Any]], cases: List[ChaosCase]) -> None:
    total = len(results)
    ok_2xx = sum(1 for r in results if 200 <= int(r["status_code"]) < 300)
    blocked_401_403 = sum(1 for r in results if int(r["status_code"]) in {401, 403})
    bad_request_400 = sum(1 for r in results if int(r["status_code"]) == 400)
    server_5xx = sum(1 for r in results if int(r["status_code"]) >= 500)
    request_errors = sum(1 for r in results if int(r["status_code"]) == 0)

    expected_block_cases = [c for c in cases if c.signature_mode in {"invalid", "missing"}]
    expected_blocked = sum(
        1
        for c, r in zip(cases, results)
        if c.signature_mode in {"invalid", "missing"} and int(r["status_code"]) in {401, 403}
    )
    interception_rate = (expected_blocked / len(expected_block_cases) * 100.0) if expected_block_cases else 0.0
    success_rate = (ok_2xx / total * 100.0) if total else 0.0

    print("\n=== Chaos Payment Test Summary ===")
    print(f"Total cases:            {total}")
    print(f"2xx success:            {ok_2xx} ({success_rate:.1f}%)")
    print(f"401/403 blocked:        {blocked_401_403}")
    print(f"400 validation reject:  {bad_request_400}")
    print(f"5xx server errors:      {server_5xx}")
    print(f"Request errors:         {request_errors}")
    print(f"Signature intercept:    {expected_blocked}/{len(expected_block_cases)} ({interception_rate:.1f}%)")

    by_category: Dict[str, Dict[str, int]] = {}
    for case, result in zip(cases, results):
        group = by_category.setdefault(case.category, {"total": 0, "2xx": 0, "4xx": 0, "5xx": 0, "err": 0})
        status = int(result["status_code"])
        group["total"] += 1
        if 200 <= status < 300:
            group["2xx"] += 1
        elif 400 <= status < 500:
            group["4xx"] += 1
        elif status >= 500:
            group["5xx"] += 1
        else:
            group["err"] += 1

    print("\nPer-category:")
    for category, stats in sorted(by_category.items()):
        print(
            f"- {category:<18} total={stats['total']:>2} "
            f"2xx={stats['2xx']:>2} 4xx={stats['4xx']:>2} 5xx={stats['5xx']:>2} err={stats['err']:>2}"
        )

    unexpected = [
        (c, r)
        for c, r in zip(cases, results)
        if c.signature_mode in {"invalid", "missing"} and int(r["status_code"]) not in {401, 403}
    ]
    if unexpected:
        print("\nPotential signature bypass cases (showing up to 10):")
        for case, result in unexpected[:10]:
            print(
                f"- {case.case_id} category={case.category} signature_mode={case.signature_mode} "
                f"status={result['status_code']} response={result['response']}"
            )


def main() -> int:
    args = parse_args()

    webhook_secret = getenv_required("PAYMENT_WEBHOOK_SECRET")
    minimax_api_key = str(os.getenv("MINIMAX_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")).strip()
    llm_timeout = float(args.llm_timeout) if args.llm_timeout is not None else float(args.timeout)

    if args.ensure_plan:
        ensure_plan(args.plan_code)

    llm_used = False
    cases: List[ChaosCase]
    if minimax_api_key:
        try:
            print("[chaos] generating cases via MiniMax LLM...")
            cases = call_minimax_for_cases(
                api_key=minimax_api_key,
                base_url=args.minimax_base_url,
                model=args.minimax_model,
                count=args.count,
                plan_code=args.plan_code,
                timeout=llm_timeout,
                trust_env=bool(args.llm_trust_env),
            )
            llm_used = True
        except Exception as exc:  # noqa: BLE001
            print(f"[chaos] MiniMax generation failed, fallback to deterministic generator: {exc}")
            cases = deterministic_cases(args.count, args.plan_code)
    else:
        print("[chaos] MINIMAX_API_KEY is missing, fallback to deterministic generator")
        cases = deterministic_cases(args.count, args.plan_code)

    seed_orders_for_cases(cases, args.plan_code)

    print(f"[chaos] case_count={len(cases)} llm_used={llm_used} target={args.webhook_url}")
    results = asyncio.run(
        run_attack(
            cases=cases,
            webhook_url=args.webhook_url,
            webhook_secret=webhook_secret,
            timeout=args.timeout,
            concurrency=args.concurrency,
        )
    )

    print_summary(results, cases)

    if args.report_json:
        report_payload = {
            "meta": {
                "llm_used": llm_used,
                "webhook_url": args.webhook_url,
                "count": len(cases),
                "concurrency": args.concurrency,
                "minimax_model": args.minimax_model,
            },
            "cases": [
                {
                    "case_id": c.case_id,
                    "category": c.category,
                    "signature_mode": c.signature_mode,
                    "note": c.note,
                    "payload": c.payload,
                }
                for c in cases
            ],
            "results": results,
        }
        out_path = Path(args.report_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[chaos] report written: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
