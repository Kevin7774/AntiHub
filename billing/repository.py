from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from .models import (
    AuthUser,
    BillingAuditLog,
    Order,
    OrderStatus,
    Plan,
    PlanEntitlement,
    PointAccount,
    PointFlow,
    PointFlowType,
    Subscription,
    SubscriptionStatus,
    Tenant,
)

_UNSET = object()


def _as_utc_aware(dt: datetime) -> datetime:
    """
    Normalize datetimes to UTC aware.

    SQLite often returns offset-naive datetimes even when SQLAlchemy models use
    DateTime(timezone=True). Treat naive values as UTC to avoid TypeError when
    comparing with timezone-aware "now".
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _merge_provider_payload(existing: Optional[str], patch: dict[str, Any]) -> str:
    """
    Merge a dict patch into the order.provider_payload field.

    Historically, provider_payload stored a raw JSON webhook payload string.
    We now store a JSON object so we can keep both:
    - checkout metadata (QR code URL) created during /billing/checkout
    - last webhook payload recorded when the order transitions

    If existing payload is not valid JSON object, preserve it under "legacy".
    """

    base: dict[str, Any] = {}
    if existing:
        raw_existing = str(existing)
        try:
            parsed = json.loads(raw_existing)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            base = dict(parsed)
        else:
            base = {"legacy": raw_existing}

    base.update(dict(patch or {}))
    return json.dumps(base, ensure_ascii=False)


class BillingStateError(RuntimeError):
    pass


class _PointConcurrencyConflict(BillingStateError):
    pass


class BillingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def count_auth_users(self, *, include_inactive: bool = True, tenant_id: Optional[str] = None) -> int:
        query = select(func.count()).select_from(AuthUser)
        if not include_inactive:
            query = query.where(AuthUser.active.is_(True))
        if tenant_id:
            query = query.where(AuthUser.tenant_id == str(tenant_id).strip())
        count = self.session.scalar(query)
        return int(count or 0)

    def count_auth_users_by_role(self, role: str, *, include_inactive: bool = True) -> int:
        normalized_role = str(role or "").strip().lower()
        if not normalized_role:
            return 0
        query = select(func.count()).select_from(AuthUser).where(AuthUser.role == normalized_role)
        if not include_inactive:
            query = query.where(AuthUser.active.is_(True))
        count = self.session.scalar(query)
        return int(count or 0)

    def get_tenant_by_id(self, tenant_id: str) -> Optional[Tenant]:
        key = str(tenant_id or "").strip()
        if not key:
            return None
        return self.session.get(Tenant, key)

    def get_tenant_by_code(self, code: str) -> Optional[Tenant]:
        normalized = str(code or "").strip().lower()
        if not normalized:
            return None
        return self.session.scalar(select(Tenant).where(Tenant.code == normalized))

    def create_tenant(
        self,
        *,
        code: str,
        name: str,
        active: bool = True,
        now: Optional[datetime] = None,
    ) -> Tenant:
        normalized_code = str(code or "").strip().lower()
        if not normalized_code:
            raise BillingStateError("tenant code is required")
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise BillingStateError("tenant name is required")
        if self.get_tenant_by_code(normalized_code):
            raise BillingStateError(f"tenant already exists: {normalized_code}")
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        tenant = Tenant(
            code=normalized_code,
            name=normalized_name,
            active=bool(active),
            created_at=current,
            updated_at=current,
        )
        self.session.add(tenant)
        self.session.flush()
        return tenant

    def get_or_create_tenant(
        self,
        *,
        code: str,
        name: str,
        active: bool = True,
        now: Optional[datetime] = None,
    ) -> Tenant:
        normalized_code = str(code or "").strip().lower()
        existing = self.get_tenant_by_code(normalized_code)
        if existing is not None:
            return existing
        return self.create_tenant(code=normalized_code, name=name, active=active, now=now)

    def list_tenants(self, *, include_inactive: bool = True) -> list[Tenant]:
        query: Select[Any] = select(Tenant).order_by(Tenant.created_at.asc())
        if not include_inactive:
            query = query.where(Tenant.active.is_(True))
        return list(self.session.scalars(query).all())

    def update_tenant(
        self,
        tenant_id: str,
        *,
        code: Optional[str] = None,
        name: Optional[str] = None,
        active: Optional[bool] = None,
        now: Optional[datetime] = None,
    ) -> Tenant:
        tenant = self.get_tenant_by_id(tenant_id)
        if tenant is None:
            raise BillingStateError(f"tenant not found: {tenant_id}")
        if code is not None:
            normalized_code = str(code or "").strip().lower()
            if not normalized_code:
                raise BillingStateError("tenant code is required")
            existing = self.get_tenant_by_code(normalized_code)
            if existing is not None and str(existing.id) != str(tenant.id):
                raise BillingStateError(f"tenant already exists: {normalized_code}")
            tenant.code = normalized_code
        if name is not None:
            normalized_name = str(name or "").strip()
            if not normalized_name:
                raise BillingStateError("tenant name is required")
            tenant.name = normalized_name
        if active is not None:
            tenant.active = bool(active)
        tenant.updated_at = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        self.session.flush()
        return tenant

    def get_auth_user(self, username: str) -> Optional[AuthUser]:
        key = str(username or "").strip()
        if not key:
            return None
        return self.session.get(AuthUser, key)

    def list_auth_users(
        self,
        *,
        include_inactive: bool = True,
        tenant_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AuthUser]:
        query: Select[Any] = select(AuthUser).order_by(AuthUser.created_at.asc(), AuthUser.username.asc())
        if not include_inactive:
            query = query.where(AuthUser.active.is_(True))
        if tenant_id:
            query = query.where(AuthUser.tenant_id == str(tenant_id).strip())
        query = query.limit(max(1, min(int(limit), 500))).offset(max(0, int(offset)))
        return list(self.session.scalars(query).all())

    def upsert_auth_user(
        self,
        *,
        username: str,
        password_hash: str,
        role: str = "user",
        active: bool = True,
        tenant_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> AuthUser:
        key = str(username or "").strip()
        if not key:
            raise BillingStateError("username is required")
        secret = str(password_hash or "").strip()
        if not secret:
            raise BillingStateError("password_hash is required")
        normalized_role = str(role or "user").strip().lower()
        if normalized_role not in {"user", "admin", "root"}:
            normalized_role = "user"
        normalized_tenant_id = str(tenant_id or "").strip() or None
        if normalized_tenant_id is not None and self.get_tenant_by_id(normalized_tenant_id) is None:
            raise BillingStateError(f"tenant not found: {normalized_tenant_id}")
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        user = self.session.get(AuthUser, key)
        if user is None:
            user = AuthUser(
                username=key,
                tenant_id=normalized_tenant_id,
                password_hash=secret,
                role=normalized_role,
                active=bool(active),
                created_at=current,
                updated_at=current,
            )
            self.session.add(user)
            self.session.flush()
            return user
        user.password_hash = secret
        user.role = normalized_role
        user.active = bool(active)
        user.tenant_id = normalized_tenant_id
        user.updated_at = current
        self.session.flush()
        return user

    def create_auth_user(
        self,
        *,
        username: str,
        password_hash: str,
        role: str = "user",
        active: bool = True,
        tenant_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> AuthUser:
        existing = self.get_auth_user(username)
        if existing is not None:
            raise BillingStateError(f"user already exists: {username}")
        return self.upsert_auth_user(
            username=username,
            password_hash=password_hash,
            role=role,
            active=active,
            tenant_id=tenant_id,
            now=now,
        )

    def update_auth_user(
        self,
        username: str,
        *,
        role: Optional[str] = None,
        active: Optional[bool] = None,
        password_hash: Optional[str] = None,
        tenant_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> AuthUser:
        user = self.get_auth_user(username)
        if user is None:
            raise BillingStateError(f"user not found: {username}")
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        if role is not None:
            normalized_role = str(role or "user").strip().lower()
            if normalized_role not in {"user", "admin", "root"}:
                raise BillingStateError(f"invalid role: {role}")
            user.role = normalized_role
        if active is not None:
            user.active = bool(active)
        if password_hash is not None:
            normalized_hash = str(password_hash or "").strip()
            if not normalized_hash:
                raise BillingStateError("password_hash cannot be empty")
            user.password_hash = normalized_hash
        if tenant_id is not None:
            normalized_tenant_id = str(tenant_id or "").strip() or None
            if normalized_tenant_id is not None and self.get_tenant_by_id(normalized_tenant_id) is None:
                raise BillingStateError(f"tenant not found: {normalized_tenant_id}")
            user.tenant_id = normalized_tenant_id
        user.updated_at = current
        self.session.flush()
        return user

    def deactivate_auth_user(self, username: str, *, now: Optional[datetime] = None) -> AuthUser:
        return self.update_auth_user(username, active=False, now=now)

    def touch_auth_user_login(self, username: str, *, now: Optional[datetime] = None) -> None:
        user = self.get_auth_user(username)
        if user is None:
            return
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        user.last_login_at = current
        user.updated_at = current
        self.session.flush()

    def create_plan(
        self,
        code: str,
        name: str,
        price_cents: int,
        monthly_points: int,
        currency: str = "usd",
        description: Optional[str] = None,
        active: bool = True,
        billing_cycle: Optional[str] = None,
        trial_days: Optional[int] = None,
        metadata_json: Optional[dict[str, Any]] = None,
    ) -> Plan:
        existing = self.get_plan_by_code(code)
        if existing:
            return existing
        plan_kwargs: dict[str, Any] = {
            "code": code,
            "name": name,
            "price_cents": price_cents,
            "monthly_points": monthly_points,
            "currency": currency,
            "description": description,
            "active": active,
        }
        if billing_cycle is not None:
            plan_kwargs["billing_cycle"] = str(billing_cycle).strip().lower()
        if trial_days is not None:
            plan_kwargs["trial_days"] = int(trial_days)
        if metadata_json is not None:
            plan_kwargs["metadata_json"] = (
                dict(metadata_json) if isinstance(metadata_json, dict) else metadata_json
            )
        plan = Plan(**plan_kwargs)
        self.session.add(plan)
        self.session.flush()
        return plan

    def get_plan_by_code(self, code: str) -> Optional[Plan]:
        return self.session.scalar(select(Plan).where(Plan.code == code))

    def get_plan_by_id(self, plan_id: str) -> Optional[Plan]:
        return self.session.get(Plan, plan_id)

    def update_plan(
        self,
        plan_id: str,
        *,
        code: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        currency: Optional[str] = None,
        price_cents: Optional[int] = None,
        monthly_points: Optional[int] = None,
        active: Optional[bool] = None,
        billing_cycle: Optional[str] = None,
        trial_days: Optional[int] = None,
        metadata_json: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Plan:
        plan = self.session.get(Plan, plan_id)
        if not plan:
            raise BillingStateError(f"plan not found: {plan_id}")

        if code is not None:
            plan.code = str(code).strip()
        if name is not None:
            plan.name = str(name).strip()
        if description is not None:
            plan.description = str(description).strip() or None
        if currency is not None:
            plan.currency = str(currency).strip().lower()
        if price_cents is not None:
            plan.price_cents = int(price_cents)
        if monthly_points is not None:
            plan.monthly_points = int(monthly_points)
        if active is not None:
            plan.active = bool(active)
        if billing_cycle is not None:
            plan.billing_cycle = str(billing_cycle).strip().lower() or None
        if trial_days is not None:
            plan.trial_days = int(trial_days)
        if metadata_json is not None:
            plan.metadata_json = dict(metadata_json) if isinstance(metadata_json, dict) else metadata_json

        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        plan.updated_at = current
        self.session.flush()
        return plan

    def list_plans(self, include_inactive: bool = True) -> list[Plan]:
        query = select(Plan).order_by(Plan.created_at.asc())
        if not include_inactive:
            query = query.where(Plan.active.is_(True))
        return list(self.session.scalars(query).all())

    def deactivate_plan(self, plan_id: str, *, now: Optional[datetime] = None) -> Plan:
        plan = self.session.get(Plan, plan_id)
        if not plan:
            raise BillingStateError(f"plan not found: {plan_id}")
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        plan.active = False
        plan.updated_at = current
        self.session.flush()
        return plan

    def get_plan_entitlement(self, entitlement_id: str) -> Optional[PlanEntitlement]:
        return self.session.get(PlanEntitlement, entitlement_id)

    def get_plan_entitlement_by_key(self, *, plan_id: str, key: str) -> Optional[PlanEntitlement]:
        normalized_plan_id = str(plan_id or "").strip()
        normalized_key = str(key or "").strip()
        if not normalized_plan_id or not normalized_key:
            return None
        return self.session.scalar(
            select(PlanEntitlement).where(
                PlanEntitlement.plan_id == normalized_plan_id,
                PlanEntitlement.key == normalized_key,
            )
        )

    def list_plan_entitlements(
        self,
        *,
        plan_id: Optional[str] = None,
        include_disabled: bool = True,
    ) -> list[PlanEntitlement]:
        query = select(PlanEntitlement).order_by(PlanEntitlement.created_at.asc(), PlanEntitlement.key.asc())
        if plan_id:
            query = query.where(PlanEntitlement.plan_id == str(plan_id).strip())
        if not include_disabled:
            query = query.where(PlanEntitlement.enabled.is_(True))
        return list(self.session.scalars(query).all())

    def create_plan_entitlement(
        self,
        *,
        plan_id: str,
        key: str,
        enabled: bool = True,
        value_json: Any = None,
        limit_value: Optional[int] = None,
        metadata_json: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> PlanEntitlement:
        normalized_plan_id = str(plan_id or "").strip()
        normalized_key = str(key or "").strip()
        if not normalized_plan_id:
            raise BillingStateError("plan_id is required")
        if not normalized_key:
            raise BillingStateError("entitlement key is required")
        if self.get_plan_by_id(normalized_plan_id) is None:
            raise BillingStateError(f"plan not found: {normalized_plan_id}")

        existing = self.get_plan_entitlement_by_key(plan_id=normalized_plan_id, key=normalized_key)
        if existing is not None:
            return existing

        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        item = PlanEntitlement(
            plan_id=normalized_plan_id,
            key=normalized_key,
            enabled=bool(enabled),
            value_json=value_json,
            limit_value=(int(limit_value) if limit_value is not None else None),
            metadata_json=(dict(metadata_json) if isinstance(metadata_json, dict) else metadata_json),
            created_at=current,
            updated_at=current,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def update_plan_entitlement(
        self,
        entitlement_id: str,
        *,
        key: Optional[str] = None,
        enabled: Optional[bool] = None,
        value_json: Any = _UNSET,
        limit_value: Any = _UNSET,
        metadata_json: Any = _UNSET,
        now: Optional[datetime] = None,
    ) -> PlanEntitlement:
        item = self.get_plan_entitlement(entitlement_id)
        if item is None:
            raise BillingStateError(f"plan entitlement not found: {entitlement_id}")

        if key is not None:
            normalized_key = str(key or "").strip()
            if not normalized_key:
                raise BillingStateError("entitlement key is required")
            item.key = normalized_key
        if enabled is not None:
            item.enabled = bool(enabled)
        if value_json is not _UNSET:
            item.value_json = value_json
        if limit_value is not _UNSET:
            item.limit_value = int(limit_value) if limit_value is not None else None
        if metadata_json is not _UNSET:
            item.metadata_json = dict(metadata_json) if isinstance(metadata_json, dict) else metadata_json
        item.updated_at = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        self.session.flush()
        return item

    def delete_plan_entitlement(self, entitlement_id: str) -> bool:
        item = self.get_plan_entitlement(entitlement_id)
        if item is None:
            return False
        self.session.delete(item)
        self.session.flush()
        return True

    def bind_user_plan(
        self,
        *,
        user_id: str,
        plan_id: str,
        duration_days: int = 30,
        auto_renew: bool = False,
        now: Optional[datetime] = None,
    ) -> Subscription:
        normalized_user_id = str(user_id or "").strip()
        normalized_plan_id = str(plan_id or "").strip()
        if not normalized_user_id:
            raise BillingStateError("user_id is required")
        if not normalized_plan_id:
            raise BillingStateError("plan_id is required")
        if self.get_auth_user(normalized_user_id) is None:
            raise BillingStateError(f"user not found: {normalized_user_id}")
        if self.get_plan_by_id(normalized_plan_id) is None:
            raise BillingStateError(f"plan not found: {normalized_plan_id}")

        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        active_subscriptions = list(
            self.session.scalars(
                select(Subscription).where(
                    Subscription.user_id == normalized_user_id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                )
            ).all()
        )
        for sub in active_subscriptions:
            sub.status = SubscriptionStatus.CANCELED
            sub.canceled_at = current
            sub.updated_at = current

        lifetime_days = max(1, int(duration_days))
        subscription = Subscription(
            user_id=normalized_user_id,
            plan_id=normalized_plan_id,
            status=SubscriptionStatus.ACTIVE,
            starts_at=current,
            expires_at=current + timedelta(days=lifetime_days),
            auto_renew=bool(auto_renew),
            created_at=current,
            updated_at=current,
        )
        self.session.add(subscription)
        self.session.flush()
        return subscription

    def list_active_subscription_user_ids_by_plan(
        self,
        *,
        plan_id: str,
        now: Optional[datetime] = None,
        limit: int = 5000,
    ) -> list[str]:
        normalized_plan_id = str(plan_id or "").strip()
        if not normalized_plan_id:
            return []
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        query = (
            select(Subscription.user_id)
            .where(
                Subscription.plan_id == normalized_plan_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > current,
            )
            .limit(max(1, min(int(limit), 20000)))
        )
        rows = self.session.scalars(query).all()
        return sorted({str(item).strip() for item in rows if str(item).strip()})

    def create_order(
        self,
        user_id: str,
        plan_id: str,
        amount_cents: int,
        currency: str = "usd",
        provider: str = "manual",
        external_order_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        provider_payload: Optional[str] = None,
    ) -> Order:
        if idempotency_key:
            existing = self.session.scalar(select(Order).where(Order.idempotency_key == idempotency_key))
            if existing:
                return existing
        if external_order_id:
            existing = self.session.scalar(select(Order).where(Order.external_order_id == external_order_id))
            if existing:
                return existing

        order = Order(
            user_id=user_id,
            plan_id=plan_id,
            amount_cents=amount_cents,
            currency=currency,
            provider=provider,
            external_order_id=external_order_id,
            idempotency_key=idempotency_key,
            provider_payload=provider_payload,
            status=OrderStatus.PENDING,
        )
        try:
            with self.session.begin_nested():
                self.session.add(order)
                self.session.flush()
        except IntegrityError:
            # Concurrent duplicate insert; fall back to the existing row.
            if idempotency_key:
                existing = self.session.scalar(select(Order).where(Order.idempotency_key == idempotency_key))
                if existing:
                    return existing
            if external_order_id:
                existing = self.session.scalar(select(Order).where(Order.external_order_id == external_order_id))
                if existing:
                    return existing
            raise
        return order

    def get_order_by_external_order_id(self, external_order_id: str) -> Optional[Order]:
        query = select(Order).options(selectinload(Order.plan)).where(Order.external_order_id == external_order_id)
        return self.session.scalar(query)

    def list_orders(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        user_id: Optional[str] = None,
        user_ids: Optional[list[str]] = None,
        status: Optional[OrderStatus] = None,
    ) -> list[Order]:
        query = select(Order).options(selectinload(Order.plan)).order_by(Order.created_at.desc())
        if user_id:
            query = query.where(Order.user_id == user_id)
        if user_ids:
            normalized = [str(item).strip() for item in user_ids if str(item).strip()]
            if normalized:
                query = query.where(Order.user_id.in_(normalized))
            else:
                return []
        if status:
            query = query.where(Order.status == status)
        query = query.limit(max(1, min(int(limit), 200))).offset(max(0, int(offset)))
        return list(self.session.scalars(query).all())

    def mark_order_paid(
        self,
        order_id: str,
        paid_at: Optional[datetime] = None,
        provider_payload: Optional[str] = None,
    ) -> Order:
        order = self.session.get(Order, order_id)
        if not order:
            raise BillingStateError(f"order not found: {order_id}")
        if order.status == OrderStatus.PAID:
            return order
        if order.status != OrderStatus.PENDING:
            raise BillingStateError(f"order {order_id} cannot be marked paid from status={order.status}")

        order.status = OrderStatus.PAID
        order.paid_at = _as_utc_aware(paid_at) if paid_at else datetime.now(timezone.utc)
        if provider_payload is not None:
            try:
                parsed = json.loads(str(provider_payload or ""))
            except Exception:
                parsed = str(provider_payload or "")
            order.provider_payload = _merge_provider_payload(order.provider_payload, {"webhook": parsed})
        self.session.flush()
        return order

    def mark_order_canceled(
        self,
        order_id: str,
        *,
        now: Optional[datetime] = None,
        provider_payload: Optional[str] = None,
    ) -> Order:
        order = self.session.get(Order, order_id)
        if not order:
            raise BillingStateError(f"order not found: {order_id}")
        if order.status == OrderStatus.CANCELED:
            return order
        if order.status != OrderStatus.PENDING:
            raise BillingStateError(f"order {order_id} cannot be canceled from status={order.status}")

        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        order.status = OrderStatus.CANCELED
        order.updated_at = current
        if provider_payload is not None:
            try:
                parsed = json.loads(str(provider_payload or ""))
            except Exception:
                parsed = str(provider_payload or "")
            order.provider_payload = _merge_provider_payload(order.provider_payload, {"webhook": parsed})
        self.session.flush()
        return order

    def mark_order_failed(
        self,
        order_id: str,
        *,
        now: Optional[datetime] = None,
        provider_payload: Optional[str] = None,
    ) -> Order:
        order = self.session.get(Order, order_id)
        if not order:
            raise BillingStateError(f"order not found: {order_id}")
        if order.status == OrderStatus.FAILED:
            return order
        if order.status != OrderStatus.PENDING:
            raise BillingStateError(f"order {order_id} cannot be marked failed from status={order.status}")

        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        order.status = OrderStatus.FAILED
        order.updated_at = current
        if provider_payload is not None:
            try:
                parsed = json.loads(str(provider_payload or ""))
            except Exception:
                parsed = str(provider_payload or "")
            order.provider_payload = _merge_provider_payload(order.provider_payload, {"webhook": parsed})
        self.session.flush()
        return order

    def mark_order_refunded(
        self,
        order_id: str,
        *,
        now: Optional[datetime] = None,
        provider_payload: Optional[str] = None,
    ) -> Order:
        order = self.session.get(Order, order_id)
        if not order:
            raise BillingStateError(f"order not found: {order_id}")
        if order.status == OrderStatus.REFUNDED:
            return order
        if order.status != OrderStatus.PAID:
            raise BillingStateError(f"order {order_id} cannot be refunded from status={order.status}")

        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        order.status = OrderStatus.REFUNDED
        order.updated_at = current
        if provider_payload is not None:
            try:
                parsed = json.loads(str(provider_payload or ""))
            except Exception:
                parsed = str(provider_payload or "")
            order.provider_payload = _merge_provider_payload(order.provider_payload, {"webhook": parsed})
        self.session.flush()
        return order

    def update_order_provider_payload(
        self,
        order_id: str,
        *,
        patch: dict[str, Any],
        now: Optional[datetime] = None,
    ) -> Order:
        """
        Merge a patch into Order.provider_payload (JSON object stored as Text).

        This is used to persist provider checkout metadata (e.g. QR code URL)
        without requiring a schema migration.
        """

        order = self.session.get(Order, order_id)
        if not order:
            raise BillingStateError(f"order not found: {order_id}")
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        order.provider_payload = _merge_provider_payload(order.provider_payload, patch)
        order.updated_at = current
        self.session.flush()
        return order

    def get_order_grant_points(self, order_id: str) -> int:
        points = self.session.scalar(
            select(func.coalesce(func.sum(PointFlow.points), 0)).where(
                PointFlow.order_id == order_id,
                PointFlow.flow_type == PointFlowType.GRANT,
            )
        )
        return int(points or 0)

    def activate_subscription_from_order(
        self,
        order_id: str,
        duration_days: int = 30,
        reset_existing_active: bool = False,
        now: Optional[datetime] = None,
    ) -> Subscription:
        order = self.session.get(Order, order_id)
        if not order:
            raise BillingStateError(f"order not found: {order_id}")
        if order.status != OrderStatus.PAID:
            raise BillingStateError(f"order {order_id} is not paid")

        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        existing_for_order = self.session.scalar(
            select(Subscription).where(Subscription.order_id == order.id).order_by(Subscription.created_at.desc()).limit(1)
        )
        if existing_for_order:
            return existing_for_order

        active_query: Select[tuple[Subscription]] = (
            select(Subscription)
            .where(
                Subscription.user_id == order.user_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
        active_sub = self.session.scalar(active_query)

        extension = timedelta(days=duration_days)
        if active_sub:
            active_expires_at = _as_utc_aware(active_sub.expires_at)
            if active_expires_at > current:
                if reset_existing_active:
                    active_sub.starts_at = current
                    active_sub.expires_at = current + extension
                else:
                    active_sub.expires_at = active_expires_at + extension
                active_sub.plan_id = order.plan_id
                active_sub.order_id = order.id
                active_sub.updated_at = current
                self.session.flush()
                return active_sub

        subscription = Subscription(
            user_id=order.user_id,
            plan_id=order.plan_id,
            order_id=order.id,
            status=SubscriptionStatus.ACTIVE,
            starts_at=current,
            expires_at=current + extension,
        )
        self.session.add(subscription)
        self.session.flush()
        return subscription

    def get_active_subscription(self, user_id: str, now: Optional[datetime] = None) -> Optional[Subscription]:
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        query = (
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > current,
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
        return self.session.scalar(query)

    def expire_due_subscriptions(self, now: Optional[datetime] = None) -> int:
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        result = self.session.execute(
            update(Subscription)
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at <= current,
            )
            .values(status=SubscriptionStatus.EXPIRED, updated_at=current)
        )
        return int(result.rowcount or 0)

    def _supports_select_for_update(self) -> bool:
        bind = self.session.get_bind()
        dialect_name = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
        return dialect_name not in {"sqlite"}

    def _sum_point_flow_balance(self, user_id: str) -> int:
        balance = self.session.scalar(
            select(func.coalesce(func.sum(PointFlow.points), 0)).where(PointFlow.user_id == user_id)
        )
        return int(balance or 0)

    def _load_point_account(self, user_id: str) -> PointAccount:
        query = select(PointAccount).where(PointAccount.user_id == user_id)
        if self._supports_select_for_update():
            query = query.with_for_update()
        account = self.session.scalar(query)
        if account is not None:
            return account
        account = PointAccount(
            user_id=user_id,
            balance=self._sum_point_flow_balance(user_id),
            version=0,
        )
        self.session.add(account)
        self.session.flush()
        return account

    def _apply_point_account_delta(
        self,
        *,
        user_id: str,
        points_delta: int,
        non_negative_required: bool,
        now: Optional[datetime] = None,
    ) -> int:
        account = self._load_point_account(user_id)
        current_balance = int(account.balance or 0)
        next_balance = current_balance + int(points_delta)
        if non_negative_required and next_balance < 0:
            raise BillingStateError("insufficient points")

        expected_version = int(account.version or 0)
        current = _as_utc_aware(now) if now else datetime.now(timezone.utc)
        result = self.session.execute(
            update(PointAccount)
            .where(
                PointAccount.user_id == user_id,
                PointAccount.version == expected_version,
            )
            .values(
                balance=next_balance,
                version=expected_version + 1,
                updated_at=current,
            )
        )
        if int(result.rowcount or 0) != 1:
            raise _PointConcurrencyConflict(f"point account version conflict for user={user_id}")
        return next_balance

    def record_point_flow(
        self,
        user_id: str,
        flow_type: PointFlowType,
        points: int,
        subscription_id: Optional[str] = None,
        order_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        note: Optional[str] = None,
    ) -> PointFlow:
        if idempotency_key:
            existing = self.session.scalar(select(PointFlow).where(PointFlow.idempotency_key == idempotency_key))
            if existing:
                return existing

        non_negative_required = flow_type == PointFlowType.CONSUME
        points_delta = int(points)
        if non_negative_required and points_delta > 0:
            points_delta = -points_delta

        for _attempt in range(5):
            try:
                with self.session.begin_nested():
                    balance_after = self._apply_point_account_delta(
                        user_id=user_id,
                        points_delta=points_delta,
                        non_negative_required=non_negative_required,
                    )
                    flow = PointFlow(
                        user_id=user_id,
                        subscription_id=subscription_id,
                        order_id=order_id,
                        flow_type=flow_type,
                        points=points_delta,
                        balance_after=balance_after,
                        idempotency_key=idempotency_key,
                        note=note,
                    )
                    self.session.add(flow)
                    self.session.flush()
                    return flow
            except _PointConcurrencyConflict:
                time.sleep(0.01)
                continue
            except IntegrityError:
                # Concurrent duplicate insert on idempotency_key; return existing.
                if idempotency_key:
                    existing = self.session.scalar(select(PointFlow).where(PointFlow.idempotency_key == idempotency_key))
                    if existing:
                        return existing
                time.sleep(0.01)
                continue
            except OperationalError as exc:
                message = str(exc).lower()
                if "database is locked" in message or "deadlock" in message:
                    time.sleep(0.02)
                    continue
                raise
        raise BillingStateError(f"point flow update conflict for user={user_id}")

    def consume_points(
        self,
        *,
        user_id: str,
        points: int,
        subscription_id: Optional[str] = None,
        order_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        note: Optional[str] = None,
    ) -> PointFlow:
        spend = abs(int(points))
        return self.record_point_flow(
            user_id=user_id,
            flow_type=PointFlowType.CONSUME,
            points=-spend,
            subscription_id=subscription_id,
            order_id=order_id,
            idempotency_key=idempotency_key,
            note=note,
        )

    def get_user_point_balance(self, user_id: str) -> int:
        account = self.session.get(PointAccount, user_id)
        if account is not None:
            return int(account.balance or 0)
        return self._sum_point_flow_balance(user_id)

    def list_point_flows(
        self,
        *,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PointFlow]:
        query = (
            select(PointFlow)
            .where(PointFlow.user_id == str(user_id))
            .order_by(PointFlow.occurred_at.desc(), PointFlow.created_at.desc())
            .limit(max(1, min(int(limit), 200)))
            .offset(max(0, int(offset)))
        )
        return list(self.session.scalars(query).all())

    def record_audit_log(
        self,
        *,
        provider: str,
        event_type: str,
        raw_payload: str,
        outcome: str,
        signature: Optional[str] = None,
        signature_valid: bool = False,
        external_event_id: Optional[str] = None,
        external_order_id: Optional[str] = None,
        detail: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
    ) -> BillingAuditLog:
        log = BillingAuditLog(
            provider=str(provider or "internal")[:32],
            event_type=str(event_type or "")[:64] or "unknown",
            external_event_id=str(external_event_id)[:128] if external_event_id else None,
            external_order_id=str(external_order_id)[:128] if external_order_id else None,
            signature=str(signature)[:512] if signature else None,
            signature_valid=bool(signature_valid),
            raw_payload=str(raw_payload or ""),
            outcome=str(outcome or "")[:32] or "unknown",
            detail=str(detail) if detail else None,
            occurred_at=_as_utc_aware(occurred_at) if occurred_at else datetime.now(timezone.utc),
        )
        self.session.add(log)
        self.session.flush()
        return log

    def list_audit_logs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        provider: Optional[str] = None,
        external_order_id: Optional[str] = None,
        external_order_ids: Optional[list[str]] = None,
        outcome: Optional[str] = None,
    ) -> list[BillingAuditLog]:
        query = select(BillingAuditLog).order_by(BillingAuditLog.occurred_at.desc())
        if provider:
            query = query.where(BillingAuditLog.provider == provider)
        if external_order_id:
            query = query.where(BillingAuditLog.external_order_id == external_order_id)
        if external_order_ids:
            normalized = [str(item).strip() for item in external_order_ids if str(item).strip()]
            if normalized:
                query = query.where(BillingAuditLog.external_order_id.in_(normalized))
            else:
                return []
        if outcome:
            query = query.where(BillingAuditLog.outcome == outcome)
        query = query.limit(max(1, min(int(limit), 200))).offset(max(0, int(offset)))
        return list(self.session.scalars(query).all())

    def get_audit_log(self, log_id: str) -> Optional[BillingAuditLog]:
        return self.session.get(BillingAuditLog, log_id)

    def get_total_revenue_cents(self) -> int:
        revenue = self.session.scalar(
            select(func.coalesce(func.sum(Order.amount_cents), 0)).where(Order.status == OrderStatus.PAID)
        )
        return int(revenue or 0)
