"""Add tenant support for auth users.

Revision ID: 20260216_0003
Revises: 20260216_0002
Create Date: 2026-02-16 22:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260216_0003"
down_revision = "20260216_0002"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in set(inspector.get_table_names())


def _column_exists(bind: sa.engine.Connection, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return False
    return any(col.get("name") == column_name for col in inspector.get_columns(table_name))


def _has_index(bind: sa.engine.Connection, table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return False
    return any(item.get("name") == index_name for item in inspector.get_indexes(table_name))


def _has_fk(bind: sa.engine.Connection, table_name: str, fk_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return False
    return any((item.get("name") or "") == fk_name for item in inspector.get_foreign_keys(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    dialect = str(bind.dialect.name or "").strip().lower()

    if not _table_exists(bind, "auth_tenants"):
        op.create_table(
            "auth_tenants",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code"),
        )
    if not _has_index(bind, "auth_tenants", op.f("ix_auth_tenants_code")):
        op.create_index(op.f("ix_auth_tenants_code"), "auth_tenants", ["code"], unique=True)
    if not _has_index(bind, "auth_tenants", op.f("ix_auth_tenants_name")):
        op.create_index(op.f("ix_auth_tenants_name"), "auth_tenants", ["name"], unique=False)
    if not _has_index(bind, "auth_tenants", op.f("ix_auth_tenants_active")):
        op.create_index(op.f("ix_auth_tenants_active"), "auth_tenants", ["active"], unique=False)

    if _table_exists(bind, "auth_users") and not _column_exists(bind, "auth_users", "tenant_id"):
        op.add_column("auth_users", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    if _table_exists(bind, "auth_users") and not _has_index(bind, "auth_users", op.f("ix_auth_users_tenant_id")):
        op.create_index(op.f("ix_auth_users_tenant_id"), "auth_users", ["tenant_id"], unique=False)
    if _table_exists(bind, "auth_users") and not _has_index(bind, "auth_users", "ix_auth_users_tenant_active"):
        op.create_index("ix_auth_users_tenant_active", "auth_users", ["tenant_id", "active"], unique=False)

    # SQLite cannot add FK constraints to existing tables via ALTER TABLE.
    if (
        dialect != "sqlite"
        and _table_exists(bind, "auth_users")
        and _column_exists(bind, "auth_users", "tenant_id")
        and not _has_fk(bind, "auth_users", "fk_auth_users_tenant_id_auth_tenants")
    ):
        op.create_foreign_key(
            "fk_auth_users_tenant_id_auth_tenants",
            "auth_users",
            "auth_tenants",
            ["tenant_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = str(bind.dialect.name or "").strip().lower()

    if _table_exists(bind, "auth_users") and _has_index(bind, "auth_users", "ix_auth_users_tenant_active"):
        op.drop_index("ix_auth_users_tenant_active", table_name="auth_users")
    if _table_exists(bind, "auth_users") and _has_index(bind, "auth_users", op.f("ix_auth_users_tenant_id")):
        op.drop_index(op.f("ix_auth_users_tenant_id"), table_name="auth_users")

    if dialect != "sqlite" and _table_exists(bind, "auth_users") and _has_fk(
        bind, "auth_users", "fk_auth_users_tenant_id_auth_tenants"
    ):
        op.drop_constraint("fk_auth_users_tenant_id_auth_tenants", "auth_users", type_="foreignkey")

    if _table_exists(bind, "auth_users") and _column_exists(bind, "auth_users", "tenant_id") and dialect != "sqlite":
        op.drop_column("auth_users", "tenant_id")

    if _table_exists(bind, "auth_tenants") and _has_index(bind, "auth_tenants", op.f("ix_auth_tenants_active")):
        op.drop_index(op.f("ix_auth_tenants_active"), table_name="auth_tenants")
    if _table_exists(bind, "auth_tenants") and _has_index(bind, "auth_tenants", op.f("ix_auth_tenants_name")):
        op.drop_index(op.f("ix_auth_tenants_name"), table_name="auth_tenants")
    if _table_exists(bind, "auth_tenants") and _has_index(bind, "auth_tenants", op.f("ix_auth_tenants_code")):
        op.drop_index(op.f("ix_auth_tenants_code"), table_name="auth_tenants")
    if _table_exists(bind, "auth_tenants"):
        op.drop_table("auth_tenants")
