"""Add SaaS plans metadata and plan entitlements.

Revision ID: 20260225_0004
Revises: 20260216_0003
Create Date: 2026-02-25 23:50:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260225_0004"
down_revision = "20260216_0003"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in set(inspector.get_table_names())


def _column_exists(bind: sa.engine.Connection, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return False
    return any(item.get("name") == column_name for item in inspector.get_columns(table_name))


def _has_index(bind: sa.engine.Connection, table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return False
    return any(item.get("name") == index_name for item in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "billing_plans") and not _column_exists(bind, "billing_plans", "billing_cycle"):
        op.add_column(
            "billing_plans",
            sa.Column("billing_cycle", sa.String(length=16), nullable=True, server_default=sa.text("'monthly'")),
        )
    if _table_exists(bind, "billing_plans") and not _column_exists(bind, "billing_plans", "trial_days"):
        op.add_column(
            "billing_plans",
            sa.Column("trial_days", sa.Integer(), nullable=True, server_default=sa.text("0")),
        )
    if _table_exists(bind, "billing_plans") and not _column_exists(bind, "billing_plans", "metadata"):
        op.add_column(
            "billing_plans",
            sa.Column("metadata", sa.JSON(), nullable=True, server_default=sa.text("'{}'")),
        )

    if not _table_exists(bind, "billing_plan_entitlements"):
        op.create_table(
            "billing_plan_entitlements",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("plan_id", sa.String(length=36), nullable=False),
            sa.Column("key", sa.String(length=128), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("value", sa.JSON(), nullable=True),
            sa.Column("limit_value", sa.Integer(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["plan_id"], ["billing_plans.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index(bind, "billing_plan_entitlements", op.f("ix_billing_plan_entitlements_plan_id")):
        op.create_index(op.f("ix_billing_plan_entitlements_plan_id"), "billing_plan_entitlements", ["plan_id"], unique=False)
    if not _has_index(bind, "billing_plan_entitlements", op.f("ix_billing_plan_entitlements_key")):
        op.create_index(op.f("ix_billing_plan_entitlements_key"), "billing_plan_entitlements", ["key"], unique=False)
    if not _has_index(bind, "billing_plan_entitlements", op.f("ix_billing_plan_entitlements_enabled")):
        op.create_index(op.f("ix_billing_plan_entitlements_enabled"), "billing_plan_entitlements", ["enabled"], unique=False)
    if not _has_index(bind, "billing_plan_entitlements", "ix_billing_plan_entitlements_plan_key"):
        op.create_index(
            "ix_billing_plan_entitlements_plan_key",
            "billing_plan_entitlements",
            ["plan_id", "key"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = str(getattr(bind.dialect, "name", "") or "").lower()

    if _table_exists(bind, "billing_plan_entitlements") and _has_index(
        bind,
        "billing_plan_entitlements",
        "ix_billing_plan_entitlements_plan_key",
    ):
        op.drop_index("ix_billing_plan_entitlements_plan_key", table_name="billing_plan_entitlements")
    if _table_exists(bind, "billing_plan_entitlements") and _has_index(
        bind,
        "billing_plan_entitlements",
        op.f("ix_billing_plan_entitlements_enabled"),
    ):
        op.drop_index(op.f("ix_billing_plan_entitlements_enabled"), table_name="billing_plan_entitlements")
    if _table_exists(bind, "billing_plan_entitlements") and _has_index(
        bind,
        "billing_plan_entitlements",
        op.f("ix_billing_plan_entitlements_key"),
    ):
        op.drop_index(op.f("ix_billing_plan_entitlements_key"), table_name="billing_plan_entitlements")
    if _table_exists(bind, "billing_plan_entitlements") and _has_index(
        bind,
        "billing_plan_entitlements",
        op.f("ix_billing_plan_entitlements_plan_id"),
    ):
        op.drop_index(op.f("ix_billing_plan_entitlements_plan_id"), table_name="billing_plan_entitlements")
    if _table_exists(bind, "billing_plan_entitlements"):
        op.drop_table("billing_plan_entitlements")

    # SQLite downgrade keeps extra columns to avoid unsupported ALTER variants.
    if dialect_name == "sqlite":
        return

    if _table_exists(bind, "billing_plans") and _column_exists(bind, "billing_plans", "metadata"):
        op.drop_column("billing_plans", "metadata")
    if _table_exists(bind, "billing_plans") and _column_exists(bind, "billing_plans", "trial_days"):
        op.drop_column("billing_plans", "trial_days")
    if _table_exists(bind, "billing_plans") and _column_exists(bind, "billing_plans", "billing_cycle"):
        op.drop_column("billing_plans", "billing_cycle")
