from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DecisionBase(DeclarativeBase):
    pass


class ProductType(str, enum.Enum):
    OPEN_SOURCE = "open_source"
    COMMERCIAL = "commercial"
    PRIVATE_SOLUTION = "private_solution"


class ProductActionType(str, enum.Enum):
    ONE_CLICK_DEPLOY = "one_click_deploy"
    VISIT_OFFICIAL_SITE = "visit_official_site"
    CONTACT_SOLUTION = "contact_solution"


class Case(DecisionBase):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(180), index=True)
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType, native_enum=False), index=True)
    action_type: Mapped[ProductActionType] = mapped_column(
        Enum(ProductActionType, native_enum=False),
        default=ProductActionType.VISIT_OFFICIAL_SITE,
    )
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    official_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    repo_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    vendor: Mapped[Optional[str]] = mapped_column(String(180), nullable=True)
    pricing_model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    estimated_monthly_cost_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    popularity_score: Mapped[int] = mapped_column(Integer, default=50)
    cost_bonus_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    capabilities: Mapped[list["CaseCapability"]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
    )
    evaluations: Mapped[list["Evaluation"]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
    )


class Capability(DecisionBase):
    __tablename__ = "capabilities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    domain: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    cases: Mapped[list["CaseCapability"]] = relationship(
        back_populates="capability",
        cascade="all, delete-orphan",
    )


class CaseCapability(DecisionBase):
    __tablename__ = "case_capabilities"

    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), primary_key=True, index=True)
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"), primary_key=True, index=True)
    weight: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    case: Mapped[Case] = relationship(back_populates="capabilities")
    capability: Mapped[Capability] = relationship(back_populates="cases")


class Evaluation(DecisionBase):
    __tablename__ = "evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    query_text: Mapped[str] = mapped_column(Text)
    requested_capabilities_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    relevance_score: Mapped[int] = mapped_column(Integer)
    popularity_score: Mapped[int] = mapped_column(Integer)
    cost_bonus_score: Mapped[int] = mapped_column(Integer)
    capability_match_score: Mapped[int] = mapped_column(Integer)
    final_score: Mapped[int] = mapped_column(Integer, index=True)
    breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    case: Mapped[Case] = relationship(back_populates="evaluations")


Index("ix_cases_product_type_active", Case.product_type, Case.active)
Index("ix_eval_case_created", Evaluation.case_id, Evaluation.created_at)
