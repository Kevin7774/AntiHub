"""Initialize billing and auth schema.

Revision ID: 20260216_0002
Revises: 20260216_0001
Create Date: 2026-02-16 00:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260216_0002"
down_revision = "20260216_0001"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in set(inspector.get_table_names())


def _has_index(bind: sa.engine.Connection, table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return False
    return any(item.get("name") == index_name for item in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "auth_users"):
        op.create_table(
            "auth_users",
            sa.Column("username", sa.String(length=128), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False, server_default=sa.text("'user'")),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("username"),
        )
    if not _has_index(bind, "auth_users", op.f("ix_auth_users_active")):
        op.create_index(op.f("ix_auth_users_active"), "auth_users", ["active"], unique=False)
    if not _has_index(bind, "auth_users", op.f("ix_auth_users_role")):
        op.create_index(op.f("ix_auth_users_role"), "auth_users", ["role"], unique=False)

    if not _table_exists(bind, "billing_plans"):
        op.create_table(
            "billing_plans",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("currency", sa.String(length=8), nullable=False, server_default=sa.text("'usd'")),
            sa.Column("price_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("monthly_points", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code"),
        )
    if not _has_index(bind, "billing_plans", op.f("ix_billing_plans_code")):
        op.create_index(op.f("ix_billing_plans_code"), "billing_plans", ["code"], unique=True)

    if not _table_exists(bind, "billing_orders"):
        op.create_table(
            "billing_orders",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("plan_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False, server_default=sa.text("'manual'")),
            sa.Column("external_order_id", sa.String(length=128), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("amount_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("currency", sa.String(length=8), nullable=False, server_default=sa.text("'usd'")),
            sa.Column(
                "status",
                sa.Enum("PENDING", "PAID", "FAILED", "CANCELED", "REFUNDED", name="orderstatus", native_enum=False),
                nullable=False,
                server_default=sa.text("'PENDING'"),
            ),
            sa.Column("provider_payload", sa.Text(), nullable=True),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["plan_id"], ["billing_plans.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("external_order_id"),
            sa.UniqueConstraint("idempotency_key"),
        )
    if not _has_index(bind, "billing_orders", op.f("ix_billing_orders_user_id")):
        op.create_index(op.f("ix_billing_orders_user_id"), "billing_orders", ["user_id"], unique=False)
    if not _has_index(bind, "billing_orders", op.f("ix_billing_orders_plan_id")):
        op.create_index(op.f("ix_billing_orders_plan_id"), "billing_orders", ["plan_id"], unique=False)
    if not _has_index(bind, "billing_orders", op.f("ix_billing_orders_external_order_id")):
        op.create_index(op.f("ix_billing_orders_external_order_id"), "billing_orders", ["external_order_id"], unique=True)
    if not _has_index(bind, "billing_orders", op.f("ix_billing_orders_idempotency_key")):
        op.create_index(op.f("ix_billing_orders_idempotency_key"), "billing_orders", ["idempotency_key"], unique=True)
    if not _has_index(bind, "billing_orders", "ix_billing_orders_user_status"):
        op.create_index("ix_billing_orders_user_status", "billing_orders", ["user_id", "status"], unique=False)

    if not _table_exists(bind, "billing_subscriptions"):
        op.create_table(
            "billing_subscriptions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("plan_id", sa.String(length=36), nullable=False),
            sa.Column("order_id", sa.String(length=36), nullable=True),
            sa.Column(
                "status",
                sa.Enum("ACTIVE", "EXPIRED", "CANCELED", name="subscriptionstatus", native_enum=False),
                nullable=False,
                server_default=sa.text("'ACTIVE'"),
            ),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["order_id"], ["billing_orders.id"]),
            sa.ForeignKeyConstraint(["plan_id"], ["billing_plans.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index(bind, "billing_subscriptions", op.f("ix_billing_subscriptions_user_id")):
        op.create_index(op.f("ix_billing_subscriptions_user_id"), "billing_subscriptions", ["user_id"], unique=False)
    if not _has_index(bind, "billing_subscriptions", op.f("ix_billing_subscriptions_plan_id")):
        op.create_index(op.f("ix_billing_subscriptions_plan_id"), "billing_subscriptions", ["plan_id"], unique=False)
    if not _has_index(bind, "billing_subscriptions", op.f("ix_billing_subscriptions_order_id")):
        op.create_index(op.f("ix_billing_subscriptions_order_id"), "billing_subscriptions", ["order_id"], unique=False)
    if not _has_index(bind, "billing_subscriptions", op.f("ix_billing_subscriptions_expires_at")):
        op.create_index(op.f("ix_billing_subscriptions_expires_at"), "billing_subscriptions", ["expires_at"], unique=False)
    if not _has_index(bind, "billing_subscriptions", "ix_billing_subscriptions_user_status"):
        op.create_index(
            "ix_billing_subscriptions_user_status",
            "billing_subscriptions",
            ["user_id", "status"],
            unique=False,
        )

    if not _table_exists(bind, "billing_point_flows"):
        op.create_table(
            "billing_point_flows",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("subscription_id", sa.String(length=36), nullable=True),
            sa.Column("order_id", sa.String(length=36), nullable=True),
            sa.Column(
                "flow_type",
                sa.Enum("GRANT", "CONSUME", "REFUND", "EXPIRE", "ADJUST", name="pointflowtype", native_enum=False),
                nullable=False,
            ),
            sa.Column("points", sa.Integer(), nullable=False),
            sa.Column("balance_after", sa.Integer(), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["order_id"], ["billing_orders.id"]),
            sa.ForeignKeyConstraint(["subscription_id"], ["billing_subscriptions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_key"),
        )
    if not _has_index(bind, "billing_point_flows", op.f("ix_billing_point_flows_user_id")):
        op.create_index(op.f("ix_billing_point_flows_user_id"), "billing_point_flows", ["user_id"], unique=False)
    if not _has_index(bind, "billing_point_flows", op.f("ix_billing_point_flows_subscription_id")):
        op.create_index(
            op.f("ix_billing_point_flows_subscription_id"),
            "billing_point_flows",
            ["subscription_id"],
            unique=False,
        )
    if not _has_index(bind, "billing_point_flows", op.f("ix_billing_point_flows_order_id")):
        op.create_index(op.f("ix_billing_point_flows_order_id"), "billing_point_flows", ["order_id"], unique=False)
    if not _has_index(bind, "billing_point_flows", op.f("ix_billing_point_flows_idempotency_key")):
        op.create_index(
            op.f("ix_billing_point_flows_idempotency_key"),
            "billing_point_flows",
            ["idempotency_key"],
            unique=True,
        )
    if not _has_index(bind, "billing_point_flows", op.f("ix_billing_point_flows_occurred_at")):
        op.create_index(op.f("ix_billing_point_flows_occurred_at"), "billing_point_flows", ["occurred_at"], unique=False)

    if not _table_exists(bind, "billing_point_accounts"):
        op.create_table(
            "billing_point_accounts",
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("balance", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("user_id"),
        )
    if not _has_index(bind, "billing_point_accounts", "ix_billing_point_accounts_balance"):
        op.create_index("ix_billing_point_accounts_balance", "billing_point_accounts", ["balance"], unique=False)

    if not _table_exists(bind, "billing_audit_logs"):
        op.create_table(
            "billing_audit_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False, server_default=sa.text("'internal'")),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("external_event_id", sa.String(length=128), nullable=True),
            sa.Column("external_order_id", sa.String(length=128), nullable=True),
            sa.Column("signature", sa.String(length=512), nullable=True),
            sa.Column("signature_valid", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("raw_payload", sa.Text(), nullable=False),
            sa.Column("outcome", sa.String(length=32), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index(bind, "billing_audit_logs", op.f("ix_billing_audit_logs_occurred_at")):
        op.create_index(op.f("ix_billing_audit_logs_occurred_at"), "billing_audit_logs", ["occurred_at"], unique=False)
    if not _has_index(bind, "billing_audit_logs", op.f("ix_billing_audit_logs_provider")):
        op.create_index(op.f("ix_billing_audit_logs_provider"), "billing_audit_logs", ["provider"], unique=False)
    if not _has_index(bind, "billing_audit_logs", op.f("ix_billing_audit_logs_event_type")):
        op.create_index(op.f("ix_billing_audit_logs_event_type"), "billing_audit_logs", ["event_type"], unique=False)
    if not _has_index(bind, "billing_audit_logs", op.f("ix_billing_audit_logs_external_event_id")):
        op.create_index(
            op.f("ix_billing_audit_logs_external_event_id"),
            "billing_audit_logs",
            ["external_event_id"],
            unique=False,
        )
    if not _has_index(bind, "billing_audit_logs", op.f("ix_billing_audit_logs_external_order_id")):
        op.create_index(
            op.f("ix_billing_audit_logs_external_order_id"),
            "billing_audit_logs",
            ["external_order_id"],
            unique=False,
        )
    if not _has_index(bind, "billing_audit_logs", op.f("ix_billing_audit_logs_outcome")):
        op.create_index(op.f("ix_billing_audit_logs_outcome"), "billing_audit_logs", ["outcome"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()

    for table_name, index_name in [
        ("billing_audit_logs", op.f("ix_billing_audit_logs_outcome")),
        ("billing_audit_logs", op.f("ix_billing_audit_logs_external_order_id")),
        ("billing_audit_logs", op.f("ix_billing_audit_logs_external_event_id")),
        ("billing_audit_logs", op.f("ix_billing_audit_logs_event_type")),
        ("billing_audit_logs", op.f("ix_billing_audit_logs_provider")),
        ("billing_audit_logs", op.f("ix_billing_audit_logs_occurred_at")),
        ("billing_point_accounts", "ix_billing_point_accounts_balance"),
        ("billing_point_flows", op.f("ix_billing_point_flows_occurred_at")),
        ("billing_point_flows", op.f("ix_billing_point_flows_idempotency_key")),
        ("billing_point_flows", op.f("ix_billing_point_flows_order_id")),
        ("billing_point_flows", op.f("ix_billing_point_flows_subscription_id")),
        ("billing_point_flows", op.f("ix_billing_point_flows_user_id")),
        ("billing_subscriptions", "ix_billing_subscriptions_user_status"),
        ("billing_subscriptions", op.f("ix_billing_subscriptions_expires_at")),
        ("billing_subscriptions", op.f("ix_billing_subscriptions_order_id")),
        ("billing_subscriptions", op.f("ix_billing_subscriptions_plan_id")),
        ("billing_subscriptions", op.f("ix_billing_subscriptions_user_id")),
        ("billing_orders", "ix_billing_orders_user_status"),
        ("billing_orders", op.f("ix_billing_orders_idempotency_key")),
        ("billing_orders", op.f("ix_billing_orders_external_order_id")),
        ("billing_orders", op.f("ix_billing_orders_plan_id")),
        ("billing_orders", op.f("ix_billing_orders_user_id")),
        ("billing_plans", op.f("ix_billing_plans_code")),
        ("auth_users", op.f("ix_auth_users_role")),
        ("auth_users", op.f("ix_auth_users_active")),
    ]:
        if _has_index(bind, table_name, index_name):
            op.drop_index(index_name, table_name=table_name)

    for table_name in [
        "billing_audit_logs",
        "billing_point_accounts",
        "billing_point_flows",
        "billing_subscriptions",
        "billing_orders",
        "billing_plans",
        "auth_users",
    ]:
        if _table_exists(bind, table_name):
            op.drop_table(table_name)
