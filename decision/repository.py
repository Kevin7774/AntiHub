from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session, selectinload

from .models import (
    Capability,
    Case,
    CaseCapability,
    Evaluation,
    ProductActionType,
    ProductType,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DecisionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_active_cases(self) -> list[Case]:
        query: Select[tuple[Case]] = (
            select(Case)
            .options(selectinload(Case.capabilities).selectinload(CaseCapability.capability))
            .where(Case.active.is_(True))
            .order_by(Case.created_at.asc())
        )
        return list(self.session.scalars(query).all())

    def get_case_by_slug(self, slug: str) -> Optional[Case]:
        query: Select[tuple[Case]] = select(Case).where(Case.slug == str(slug or "").strip())
        return self.session.scalar(query)

    def get_case_by_id(self, case_id: str) -> Optional[Case]:
        query: Select[tuple[Case]] = (
            select(Case)
            .options(selectinload(Case.capabilities).selectinload(CaseCapability.capability))
            .where(Case.id == case_id)
        )
        return self.session.scalar(query)

    def upsert_capability(
        self,
        *,
        code: str,
        name: str,
        description: str = "",
        aliases: Iterable[str] | None = None,
        domain: str = "general",
        active: bool = True,
    ) -> Capability:
        normalized_code = str(code or "").strip().lower()
        if not normalized_code:
            raise ValueError("capability code is required")
        capability = self.session.scalar(select(Capability).where(Capability.code == normalized_code))
        alias_list = [str(item).strip().lower() for item in (aliases or []) if str(item).strip()]
        if capability:
            capability.name = str(name or capability.name).strip() or capability.name
            capability.description = str(description or "").strip() or capability.description
            capability.aliases_json = alias_list
            capability.domain = str(domain or "").strip() or capability.domain
            capability.active = bool(active)
            capability.updated_at = _utc_now()
            self.session.flush()
            return capability

        capability = Capability(
            code=normalized_code,
            name=str(name).strip(),
            description=str(description or "").strip() or None,
            aliases_json=alias_list,
            domain=str(domain or "").strip() or None,
            active=bool(active),
        )
        self.session.add(capability)
        self.session.flush()
        return capability

    def list_capabilities(self, include_inactive: bool = False) -> list[Capability]:
        query: Select[tuple[Capability]] = select(Capability).order_by(Capability.code.asc())
        if not include_inactive:
            query = query.where(Capability.active.is_(True))
        return list(self.session.scalars(query).all())

    def upsert_case(
        self,
        *,
        slug: str,
        title: str,
        product_type: ProductType,
        action_type: ProductActionType,
        summary: str = "",
        official_url: Optional[str] = None,
        repo_url: Optional[str] = None,
        vendor: Optional[str] = None,
        pricing_model: Optional[str] = None,
        estimated_monthly_cost_cents: Optional[int] = None,
        popularity_score: int = 50,
        cost_bonus_override: Optional[int] = None,
        metadata_json: Optional[dict[str, Any]] = None,
        active: bool = True,
        capability_codes: Iterable[str] | None = None,
    ) -> Case:
        normalized_slug = str(slug or "").strip().lower()
        if not normalized_slug:
            raise ValueError("case slug is required")
        normalized_title = str(title or "").strip()
        if not normalized_title:
            raise ValueError("case title is required")

        row = self.get_case_by_slug(normalized_slug)
        if not row:
            row = Case(
                slug=normalized_slug,
                title=normalized_title,
                product_type=product_type,
                action_type=action_type,
                summary=str(summary or "").strip() or None,
                official_url=str(official_url or "").strip() or None,
                repo_url=str(repo_url or "").strip() or None,
                vendor=str(vendor or "").strip() or None,
                pricing_model=str(pricing_model or "").strip() or None,
                estimated_monthly_cost_cents=estimated_monthly_cost_cents,
                popularity_score=max(0, min(100, int(popularity_score))),
                cost_bonus_override=cost_bonus_override,
                metadata_json=dict(metadata_json or {}),
                active=bool(active),
            )
            self.session.add(row)
            self.session.flush()
        else:
            row.title = normalized_title
            row.product_type = product_type
            row.action_type = action_type
            row.summary = str(summary or "").strip() or None
            row.official_url = str(official_url or "").strip() or None
            row.repo_url = str(repo_url or "").strip() or None
            row.vendor = str(vendor or "").strip() or None
            row.pricing_model = str(pricing_model or "").strip() or None
            row.estimated_monthly_cost_cents = estimated_monthly_cost_cents
            row.popularity_score = max(0, min(100, int(popularity_score)))
            row.cost_bonus_override = cost_bonus_override
            row.metadata_json = dict(metadata_json or {})
            row.active = bool(active)
            row.updated_at = _utc_now()
            self.session.flush()

        if capability_codes is not None:
            self._replace_case_capabilities(row.id, capability_codes)
        return row

    def _replace_case_capabilities(self, case_id: str, capability_codes: Iterable[str]) -> None:
        desired_codes = {
            str(item or "").strip().lower()
            for item in capability_codes
            if str(item or "").strip()
        }
        self.session.execute(delete(CaseCapability).where(CaseCapability.case_id == case_id))
        if not desired_codes:
            self.session.flush()
            return

        capabilities = self.session.scalars(select(Capability).where(Capability.code.in_(desired_codes))).all()
        for capability in capabilities:
            link = CaseCapability(case_id=case_id, capability_id=capability.id, weight=100)
            self.session.add(link)
        self.session.flush()

    def create_evaluation(
        self,
        *,
        case_id: str,
        query_text: str,
        requested_capabilities: list[str],
        relevance_score: int,
        popularity_score: int,
        cost_bonus_score: int,
        capability_match_score: int,
        final_score: int,
        breakdown: dict[str, Any] | None = None,
    ) -> Evaluation:
        row = Evaluation(
            case_id=case_id,
            query_text=str(query_text or "").strip(),
            requested_capabilities_json=[str(item) for item in requested_capabilities],
            relevance_score=max(0, min(100, int(relevance_score))),
            popularity_score=max(0, min(100, int(popularity_score))),
            cost_bonus_score=max(0, min(100, int(cost_bonus_score))),
            capability_match_score=max(0, min(100, int(capability_match_score))),
            final_score=max(0, min(100, int(final_score))),
            breakdown_json=dict(breakdown or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row
