from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "auth_tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    users: Mapped[list["AuthUser"]] = relationship(back_populates="tenant")


class AuthUser(Base):
    __tablename__ = "auth_users"

    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(ForeignKey("auth_tenants.id"), nullable=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    tenant: Mapped[Optional[Tenant]] = relationship(back_populates="users")


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELED = "canceled"
    REFUNDED = "refunded"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELED = "canceled"


class PointFlowType(str, enum.Enum):
    GRANT = "grant"
    CONSUME = "consume"
    REFUND = "refund"
    EXPIRE = "expire"
    ADJUST = "adjust"


class Plan(Base):
    __tablename__ = "billing_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    monthly_points: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    orders: Mapped[list["Order"]] = relationship(back_populates="plan")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")


class Order(Base):
    __tablename__ = "billing_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("billing_plans.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="manual")
    external_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, unique=True, index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, unique=True, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, native_enum=False), default=OrderStatus.PENDING)
    provider_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    plan: Mapped[Plan] = relationship(back_populates="orders")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="order")
    point_flows: Mapped[list["PointFlow"]] = relationship(back_populates="order")


class Subscription(Base):
    __tablename__ = "billing_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("billing_plans.id"), index=True)
    order_id: Mapped[Optional[str]] = mapped_column(ForeignKey("billing_orders.id"), nullable=True, index=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False), default=SubscriptionStatus.ACTIVE
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    canceled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    plan: Mapped[Plan] = relationship(back_populates="subscriptions")
    order: Mapped[Optional[Order]] = relationship(back_populates="subscriptions")
    point_flows: Mapped[list["PointFlow"]] = relationship(back_populates="subscription")


class PointFlow(Base):
    __tablename__ = "billing_point_flows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("billing_subscriptions.id"), nullable=True, index=True
    )
    order_id: Mapped[Optional[str]] = mapped_column(ForeignKey("billing_orders.id"), nullable=True, index=True)
    flow_type: Mapped[PointFlowType] = mapped_column(Enum(PointFlowType, native_enum=False))
    points: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, unique=True, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    subscription: Mapped[Optional[Subscription]] = relationship(back_populates="point_flows")
    order: Mapped[Optional[Order]] = relationship(back_populates="point_flows")


class PointAccount(Base):
    __tablename__ = "billing_point_accounts"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BillingAuditLog(Base):
    """
    Financial-grade audit log.

    This is append-only and records every webhook attempt (including failures).
    Store raw payload for dispute resolution.
    """

    __tablename__ = "billing_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="internal", index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    external_event_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    external_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    signature: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_payload: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


Index("ix_billing_subscriptions_user_status", Subscription.user_id, Subscription.status)
Index("ix_billing_orders_user_status", Order.user_id, Order.status)
Index("ix_billing_point_accounts_balance", PointAccount.balance)
Index("ix_auth_users_tenant_active", AuthUser.tenant_id, AuthUser.active)
