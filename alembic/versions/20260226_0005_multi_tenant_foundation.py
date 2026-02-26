"""Add multi-tenant foundation scaffold tables.

Revision ID: 20260226_0005
Revises: 20260225_0004
Create Date: 2026-02-26 12:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260226_0005"
down_revision = "20260225_0004"
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

    if not _table_exists(bind, "auth_tenant_members"):
        op.create_table(
            "auth_tenant_members",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("tenant_id", sa.String(length=36), nullable=False),
            sa.Column("username", sa.String(length=128), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False, server_default=sa.text("'member'")),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["auth_tenants.id"]),
            sa.ForeignKeyConstraint(["username"], ["auth_users.username"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index(bind, "auth_tenant_members", op.f("ix_auth_tenant_members_tenant_id")):
        op.create_index(op.f("ix_auth_tenant_members_tenant_id"), "auth_tenant_members", ["tenant_id"], unique=False)
    if not _has_index(bind, "auth_tenant_members", op.f("ix_auth_tenant_members_username")):
        op.create_index(op.f("ix_auth_tenant_members_username"), "auth_tenant_members", ["username"], unique=False)
    if not _has_index(bind, "auth_tenant_members", op.f("ix_auth_tenant_members_role")):
        op.create_index(op.f("ix_auth_tenant_members_role"), "auth_tenant_members", ["role"], unique=False)
    if not _has_index(bind, "auth_tenant_members", op.f("ix_auth_tenant_members_active")):
        op.create_index(op.f("ix_auth_tenant_members_active"), "auth_tenant_members", ["active"], unique=False)
    if not _has_index(bind, "auth_tenant_members", op.f("ix_auth_tenant_members_is_default")):
        op.create_index(op.f("ix_auth_tenant_members_is_default"), "auth_tenant_members", ["is_default"], unique=False)
    if not _has_index(bind, "auth_tenant_members", "ix_auth_tenant_members_tenant_user"):
        op.create_index(
            "ix_auth_tenant_members_tenant_user",
            "auth_tenant_members",
            ["tenant_id", "username"],
            unique=True,
        )
    if not _has_index(bind, "auth_tenant_members", "ix_auth_tenant_members_username_active"):
        op.create_index(
            "ix_auth_tenant_members_username_active",
            "auth_tenant_members",
            ["username", "active"],
            unique=False,
        )

    if not _table_exists(bind, "auth_tenant_settings"):
        op.create_table(
            "auth_tenant_settings",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("tenant_id", sa.String(length=36), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("value", sa.JSON(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["auth_tenants.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index(bind, "auth_tenant_settings", op.f("ix_auth_tenant_settings_tenant_id")):
        op.create_index(op.f("ix_auth_tenant_settings_tenant_id"), "auth_tenant_settings", ["tenant_id"], unique=False)
    if not _has_index(bind, "auth_tenant_settings", op.f("ix_auth_tenant_settings_key")):
        op.create_index(op.f("ix_auth_tenant_settings_key"), "auth_tenant_settings", ["key"], unique=False)
    if not _has_index(bind, "auth_tenant_settings", "ix_auth_tenant_settings_tenant_key"):
        op.create_index(
            "ix_auth_tenant_settings_tenant_key",
            "auth_tenant_settings",
            ["tenant_id", "key"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "auth_tenant_settings") and _has_index(
        bind,
        "auth_tenant_settings",
        "ix_auth_tenant_settings_tenant_key",
    ):
        op.drop_index("ix_auth_tenant_settings_tenant_key", table_name="auth_tenant_settings")
    if _table_exists(bind, "auth_tenant_settings") and _has_index(
        bind,
        "auth_tenant_settings",
        op.f("ix_auth_tenant_settings_key"),
    ):
        op.drop_index(op.f("ix_auth_tenant_settings_key"), table_name="auth_tenant_settings")
    if _table_exists(bind, "auth_tenant_settings") and _has_index(
        bind,
        "auth_tenant_settings",
        op.f("ix_auth_tenant_settings_tenant_id"),
    ):
        op.drop_index(op.f("ix_auth_tenant_settings_tenant_id"), table_name="auth_tenant_settings")
    if _table_exists(bind, "auth_tenant_settings"):
        op.drop_table("auth_tenant_settings")

    if _table_exists(bind, "auth_tenant_members") and _has_index(
        bind,
        "auth_tenant_members",
        "ix_auth_tenant_members_username_active",
    ):
        op.drop_index("ix_auth_tenant_members_username_active", table_name="auth_tenant_members")
    if _table_exists(bind, "auth_tenant_members") and _has_index(
        bind,
        "auth_tenant_members",
        "ix_auth_tenant_members_tenant_user",
    ):
        op.drop_index("ix_auth_tenant_members_tenant_user", table_name="auth_tenant_members")
    if _table_exists(bind, "auth_tenant_members") and _has_index(
        bind,
        "auth_tenant_members",
        op.f("ix_auth_tenant_members_is_default"),
    ):
        op.drop_index(op.f("ix_auth_tenant_members_is_default"), table_name="auth_tenant_members")
    if _table_exists(bind, "auth_tenant_members") and _has_index(
        bind,
        "auth_tenant_members",
        op.f("ix_auth_tenant_members_active"),
    ):
        op.drop_index(op.f("ix_auth_tenant_members_active"), table_name="auth_tenant_members")
    if _table_exists(bind, "auth_tenant_members") and _has_index(
        bind,
        "auth_tenant_members",
        op.f("ix_auth_tenant_members_role"),
    ):
        op.drop_index(op.f("ix_auth_tenant_members_role"), table_name="auth_tenant_members")
    if _table_exists(bind, "auth_tenant_members") and _has_index(
        bind,
        "auth_tenant_members",
        op.f("ix_auth_tenant_members_username"),
    ):
        op.drop_index(op.f("ix_auth_tenant_members_username"), table_name="auth_tenant_members")
    if _table_exists(bind, "auth_tenant_members") and _has_index(
        bind,
        "auth_tenant_members",
        op.f("ix_auth_tenant_members_tenant_id"),
    ):
        op.drop_index(op.f("ix_auth_tenant_members_tenant_id"), table_name="auth_tenant_members")
    if _table_exists(bind, "auth_tenant_members"):
        op.drop_table("auth_tenant_members")
