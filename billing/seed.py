from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import SessionFactory, session_scope
from .repository import BillingRepository


@dataclass(frozen=True)
class SeedEntitlement:
    key: str
    enabled: bool = True
    value_json: Any = None
    limit_value: int | None = None
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class SeedPlan:
    code: str
    name: str
    description: str
    currency: str
    price_cents: int
    monthly_points: int
    billing_cycle: str
    duration_days: int
    trial_days: int
    metadata_json: dict[str, Any]
    entitlements: tuple[SeedEntitlement, ...]


SEED_VERSION = 1

DEFAULT_SAAS_PLANS: tuple[SeedPlan, ...] = (
    SeedPlan(
        code="monthly_198",
        name="月度订阅",
        description="适合个人与小团队的高频日常使用",
        currency="cny",
        price_cents=19800,
        monthly_points=10000,
        billing_cycle="monthly",
        duration_days=30,
        trial_days=0,
        metadata_json={
            "seed_version": SEED_VERSION,
            "display_price_cny": 198,
            "credits_per_cycle": 10000,
            "duration_days": 30,
        },
        entitlements=(
            SeedEntitlement(
                key="feature.workspace.basic",
                enabled=True,
                value_json={"tier": "standard"},
                metadata_json={"source": "seed"},
            ),
            SeedEntitlement(
                key="api.rpm",
                enabled=True,
                limit_value=60,
                value_json={"unit": "requests/minute"},
                metadata_json={"source": "seed"},
            ),
            SeedEntitlement(
                key="feature.deep_search",
                enabled=False,
                value_json={"mode": "off"},
                metadata_json={"source": "seed"},
            ),
        ),
    ),
    SeedPlan(
        code="quarterly_398",
        name="季度订阅",
        description="默认热卖档，兼顾成本与产出效率",
        currency="cny",
        price_cents=39800,
        monthly_points=30000,
        billing_cycle="quarterly",
        duration_days=90,
        trial_days=0,
        metadata_json={
            "seed_version": SEED_VERSION,
            "display_price_cny": 398,
            "credits_per_cycle": 30000,
            "duration_days": 90,
            "badge": "hot",
        },
        entitlements=(
            SeedEntitlement(
                key="feature.workspace.basic",
                enabled=True,
                value_json={"tier": "pro"},
                metadata_json={"source": "seed"},
            ),
            SeedEntitlement(
                key="api.rpm",
                enabled=True,
                limit_value=120,
                value_json={"unit": "requests/minute"},
                metadata_json={"source": "seed"},
            ),
            SeedEntitlement(
                key="feature.deep_search",
                enabled=True,
                limit_value=300,
                value_json={"mode": "standard"},
                metadata_json={"source": "seed"},
            ),
        ),
    ),
    SeedPlan(
        code="yearly_1980",
        name="年度订阅",
        description="面向高强度生产环境与管理层决策场景",
        currency="cny",
        price_cents=198000,
        monthly_points=150000,
        billing_cycle="yearly",
        duration_days=365,
        trial_days=0,
        metadata_json={
            "seed_version": SEED_VERSION,
            "display_price_cny": 1980,
            "credits_per_cycle": 150000,
            "duration_days": 365,
            "badge": "best_value",
        },
        entitlements=(
            SeedEntitlement(
                key="feature.workspace.basic",
                enabled=True,
                value_json={"tier": "enterprise"},
                metadata_json={"source": "seed"},
            ),
            SeedEntitlement(
                key="api.rpm",
                enabled=True,
                limit_value=300,
                value_json={"unit": "requests/minute"},
                metadata_json={"source": "seed"},
            ),
            SeedEntitlement(
                key="feature.deep_search",
                enabled=True,
                limit_value=1000,
                value_json={"mode": "priority"},
                metadata_json={"source": "seed"},
            ),
        ),
    ),
)


def _merge_metadata(
    existing: dict[str, Any] | None,
    defaults: dict[str, Any] | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(existing, dict):
        merged.update(existing)
    if isinstance(defaults, dict):
        for key, value in defaults.items():
            merged.setdefault(key, value)
    return merged


def seed_default_plans(*, session_factory: SessionFactory | None = None) -> dict[str, int]:
    """
    Ensure the default SaaS plans and base entitlements exist.

    Idempotency strategy:
    - Plan rows are keyed by immutable `code`.
    - Existing plans are not force-overwritten (safe for operator edits);
      only critical missing fields and metadata defaults are filled.
    - Entitlements are keyed by `(plan_id, key)`, created only when missing.
    """

    plans_created = 0
    plans_updated = 0
    entitlements_created = 0
    entitlements_updated = 0

    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        for plan_seed in DEFAULT_SAAS_PLANS:
            plan = repo.get_plan_by_code(plan_seed.code)
            if plan is None:
                plan = repo.create_plan(
                    code=plan_seed.code,
                    name=plan_seed.name,
                    description=plan_seed.description,
                    currency=plan_seed.currency,
                    price_cents=plan_seed.price_cents,
                    monthly_points=plan_seed.monthly_points,
                    billing_cycle=plan_seed.billing_cycle,
                    trial_days=plan_seed.trial_days,
                    metadata_json=dict(plan_seed.metadata_json),
                    active=True,
                )
                plans_created += 1
            else:
                update_kwargs: dict[str, Any] = {}
                if not str(getattr(plan, "name", "") or "").strip():
                    update_kwargs["name"] = plan_seed.name
                if not str(getattr(plan, "description", "") or "").strip():
                    update_kwargs["description"] = plan_seed.description
                if not str(getattr(plan, "currency", "") or "").strip():
                    update_kwargs["currency"] = plan_seed.currency
                if int(getattr(plan, "price_cents", 0) or 0) <= 0:
                    update_kwargs["price_cents"] = plan_seed.price_cents
                if int(getattr(plan, "monthly_points", 0) or 0) <= 0:
                    update_kwargs["monthly_points"] = plan_seed.monthly_points
                if not str(getattr(plan, "billing_cycle", "") or "").strip():
                    update_kwargs["billing_cycle"] = plan_seed.billing_cycle
                if getattr(plan, "trial_days", None) is None:
                    update_kwargs["trial_days"] = plan_seed.trial_days

                merged_metadata = _merge_metadata(
                    getattr(plan, "metadata_json", None),
                    plan_seed.metadata_json,
                )
                if merged_metadata != (getattr(plan, "metadata_json", None) or {}):
                    update_kwargs["metadata_json"] = merged_metadata

                if update_kwargs:
                    plan = repo.update_plan(str(plan.id), **update_kwargs)
                    plans_updated += 1

            plan_id = str(plan.id)
            for entitlement_seed in plan_seed.entitlements:
                entitlement = repo.get_plan_entitlement_by_key(
                    plan_id=plan_id,
                    key=entitlement_seed.key,
                )
                if entitlement is None:
                    repo.create_plan_entitlement(
                        plan_id=plan_id,
                        key=entitlement_seed.key,
                        enabled=entitlement_seed.enabled,
                        value_json=entitlement_seed.value_json,
                        limit_value=entitlement_seed.limit_value,
                        metadata_json=entitlement_seed.metadata_json or {},
                    )
                    entitlements_created += 1
                    continue

                entitlement_update_kwargs: dict[str, Any] = {}
                if getattr(entitlement, "value_json", None) is None and entitlement_seed.value_json is not None:
                    entitlement_update_kwargs["value_json"] = entitlement_seed.value_json
                if getattr(entitlement, "limit_value", None) is None and entitlement_seed.limit_value is not None:
                    entitlement_update_kwargs["limit_value"] = entitlement_seed.limit_value
                merged_entitlement_metadata = _merge_metadata(
                    getattr(entitlement, "metadata_json", None),
                    entitlement_seed.metadata_json or {},
                )
                if merged_entitlement_metadata != (getattr(entitlement, "metadata_json", None) or {}):
                    entitlement_update_kwargs["metadata_json"] = merged_entitlement_metadata

                if entitlement_update_kwargs:
                    repo.update_plan_entitlement(str(entitlement.id), **entitlement_update_kwargs)
                    entitlements_updated += 1

    return {
        "plans_created": plans_created,
        "plans_updated": plans_updated,
        "entitlements_created": entitlements_created,
        "entitlements_updated": entitlements_updated,
    }

