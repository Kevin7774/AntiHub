#!/usr/bin/env python3
"""
Apply AntiHub commercial pricing to the local billing database.

This script is intentionally a *tool* (not run automatically at startup):
- avoids overriding admin-configured prices on every deploy
- makes the pricing change explicit and auditable

Defaults (CNY):
- Monthly: 198 RMB
- Quarterly: 398 RMB
- Yearly: 1980 RMB

It creates or updates these plan codes:
- commercial_monthly
- commercial_quarterly
- commercial_yearly
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make project modules importable when script is run from tools/.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from billing import BillingRepository, session_scope  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply AntiHub commercial pricing to billing plans")
    parser.add_argument("--monthly", type=int, default=198, help="Monthly price in RMB (yuan)")
    parser.add_argument("--quarterly", type=int, default=398, help="Quarterly price in RMB (yuan)")
    parser.add_argument("--yearly", type=int, default=1980, help="Yearly price in RMB (yuan)")
    parser.add_argument(
        "--monthly-points",
        type=int,
        default=1000,
        help="Points granted per period for the monthly plan (quarterly/yearly scale automatically)",
    )
    parser.add_argument("--currency", default="cny", help="Plan currency code (default: cny)")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to DB")
    parser.add_argument(
        "--strict-cleanup",
        action="store_true",
        help="Deactivate plans not in commercial_monthly/commercial_quarterly/commercial_yearly",
    )
    return parser.parse_args()


def yuan_to_cents(yuan: int) -> int:
    return int(yuan) * 100


def upsert_plan(
    repo: BillingRepository,
    *,
    code: str,
    name: str,
    price_cents: int,
    points: int,
    currency: str,
    description: str,
    dry_run: bool,
) -> None:
    existing = repo.get_plan_by_code(code)
    if existing:
        before = (existing.price_cents, existing.monthly_points, existing.currency, existing.name, existing.active)
        after = (int(price_cents), int(points), str(currency).strip().lower(), str(name).strip(), True)
        if before == after:
            print(f"[skip] {code} price={existing.price_cents} points={existing.monthly_points} {existing.currency}")
            return
        print(f"[update] {code} {before} -> {after}")
        if dry_run:
            return
        repo.update_plan(
            existing.id,
            name=name,
            price_cents=int(price_cents),
            monthly_points=int(points),
            currency=str(currency).strip().lower(),
            description=description,
            active=True,
        )
        return

    print(f"[create] {code} price={price_cents} points={points} currency={currency}")
    if dry_run:
        return
    repo.create_plan(
        code=code,
        name=name,
        price_cents=int(price_cents),
        monthly_points=int(points),
        currency=str(currency).strip().lower(),
        description=description,
        active=True,
    )


def cleanup_non_commercial_plans(repo: BillingRepository, *, keep_codes: set[str], dry_run: bool) -> None:
    plans = repo.list_plans(include_inactive=True)
    for plan in plans:
        code = str(getattr(plan, "code", "") or "").strip()
        if not code or code in keep_codes:
            continue
        is_active = bool(getattr(plan, "active", False))
        if not is_active:
            continue
        print(f"[deactivate] {code}")
        if dry_run:
            continue
        repo.update_plan(str(getattr(plan, "id")), active=False)


def main() -> int:
    args = parse_args()

    currency = str(args.currency or "cny").strip().lower() or "cny"
    monthly_cents = yuan_to_cents(args.monthly)
    quarterly_cents = yuan_to_cents(args.quarterly)
    yearly_cents = yuan_to_cents(args.yearly)

    monthly_points = int(args.monthly_points)
    quarterly_points = monthly_points * 3
    yearly_points = monthly_points * 12

    description = "managed by tools/apply_pricing.py"
    keep_codes = {"commercial_monthly", "commercial_quarterly", "commercial_yearly"}

    with session_scope() as session:
        repo = BillingRepository(session)
        upsert_plan(
            repo,
            code="commercial_monthly",
            name="月付会员",
            price_cents=monthly_cents,
            points=monthly_points,
            currency=currency,
            description=description,
            dry_run=args.dry_run,
        )
        upsert_plan(
            repo,
            code="commercial_quarterly",
            name="季付会员",
            price_cents=quarterly_cents,
            points=quarterly_points,
            currency=currency,
            description=description,
            dry_run=args.dry_run,
        )
        upsert_plan(
            repo,
            code="commercial_yearly",
            name="年付会员",
            price_cents=yearly_cents,
            points=yearly_points,
            currency=currency,
            description=description,
            dry_run=args.dry_run,
        )
        if args.strict_cleanup:
            cleanup_non_commercial_plans(repo, keep_codes=keep_codes, dry_run=args.dry_run)

    print("[ok] pricing applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
