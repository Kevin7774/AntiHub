"""Initialize decision engine schema.

Revision ID: 20260216_0001
Revises:
Create Date: 2026-02-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260216_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    def has_index(table: str, index_name: str) -> bool:
        if table not in set(inspector.get_table_names()):
            return False
        return any(item.get("name") == index_name for item in inspector.get_indexes(table))

    if "capabilities" not in existing_tables:
        op.create_table(
            "capabilities",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("code", sa.String(length=96), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("aliases_json", sa.JSON(), nullable=False),
            sa.Column("domain", sa.String(length=120), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not has_index("capabilities", op.f("ix_capabilities_active")):
        op.create_index(op.f("ix_capabilities_active"), "capabilities", ["active"], unique=False)
    if not has_index("capabilities", op.f("ix_capabilities_code")):
        op.create_index(op.f("ix_capabilities_code"), "capabilities", ["code"], unique=True)
    if not has_index("capabilities", op.f("ix_capabilities_name")):
        op.create_index(op.f("ix_capabilities_name"), "capabilities", ["name"], unique=False)

    if "cases" not in existing_tables:
        op.create_table(
            "cases",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("slug", sa.String(length=96), nullable=False),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column(
                "product_type",
                sa.Enum("OPEN_SOURCE", "COMMERCIAL", "PRIVATE_SOLUTION", name="producttype", native_enum=False),
                nullable=False,
            ),
            sa.Column(
                "action_type",
                sa.Enum(
                    "ONE_CLICK_DEPLOY",
                    "VISIT_OFFICIAL_SITE",
                    "CONTACT_SOLUTION",
                    name="productactiontype",
                    native_enum=False,
                ),
                nullable=False,
            ),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("official_url", sa.String(length=512), nullable=True),
            sa.Column("repo_url", sa.String(length=512), nullable=True),
            sa.Column("vendor", sa.String(length=180), nullable=True),
            sa.Column("pricing_model", sa.String(length=120), nullable=True),
            sa.Column("estimated_monthly_cost_cents", sa.Integer(), nullable=True),
            sa.Column("popularity_score", sa.Integer(), nullable=False),
            sa.Column("cost_bonus_override", sa.Integer(), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not has_index("cases", op.f("ix_cases_active")):
        op.create_index(op.f("ix_cases_active"), "cases", ["active"], unique=False)
    if not has_index("cases", op.f("ix_cases_created_at")):
        op.create_index(op.f("ix_cases_created_at"), "cases", ["created_at"], unique=False)
    if not has_index("cases", op.f("ix_cases_product_type")):
        op.create_index(op.f("ix_cases_product_type"), "cases", ["product_type"], unique=False)
    if not has_index("cases", op.f("ix_cases_slug")):
        op.create_index(op.f("ix_cases_slug"), "cases", ["slug"], unique=True)
    if not has_index("cases", op.f("ix_cases_title")):
        op.create_index(op.f("ix_cases_title"), "cases", ["title"], unique=False)
    if not has_index("cases", "ix_cases_product_type_active"):
        op.create_index("ix_cases_product_type_active", "cases", ["product_type", "active"], unique=False)

    if "case_capabilities" not in existing_tables:
        op.create_table(
            "case_capabilities",
            sa.Column("case_id", sa.String(length=36), nullable=False),
            sa.Column("capability_id", sa.String(length=36), nullable=False),
            sa.Column("weight", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["capability_id"], ["capabilities.id"]),
            sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
            sa.PrimaryKeyConstraint("case_id", "capability_id"),
        )
    if not has_index("case_capabilities", op.f("ix_case_capabilities_capability_id")):
        op.create_index(op.f("ix_case_capabilities_capability_id"), "case_capabilities", ["capability_id"], unique=False)
    if not has_index("case_capabilities", op.f("ix_case_capabilities_case_id")):
        op.create_index(op.f("ix_case_capabilities_case_id"), "case_capabilities", ["case_id"], unique=False)

    if "evaluations" not in existing_tables:
        op.create_table(
            "evaluations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("case_id", sa.String(length=36), nullable=False),
            sa.Column("query_text", sa.Text(), nullable=False),
            sa.Column("requested_capabilities_json", sa.JSON(), nullable=False),
            sa.Column("relevance_score", sa.Integer(), nullable=False),
            sa.Column("popularity_score", sa.Integer(), nullable=False),
            sa.Column("cost_bonus_score", sa.Integer(), nullable=False),
            sa.Column("capability_match_score", sa.Integer(), nullable=False),
            sa.Column("final_score", sa.Integer(), nullable=False),
            sa.Column("breakdown_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not has_index("evaluations", op.f("ix_evaluations_case_id")):
        op.create_index(op.f("ix_evaluations_case_id"), "evaluations", ["case_id"], unique=False)
    if not has_index("evaluations", op.f("ix_evaluations_created_at")):
        op.create_index(op.f("ix_evaluations_created_at"), "evaluations", ["created_at"], unique=False)
    if not has_index("evaluations", op.f("ix_evaluations_final_score")):
        op.create_index(op.f("ix_evaluations_final_score"), "evaluations", ["final_score"], unique=False)
    if not has_index("evaluations", "ix_eval_case_created"):
        op.create_index("ix_eval_case_created", "evaluations", ["case_id", "created_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    def has_index(table: str, index_name: str) -> bool:
        if table not in set(inspector.get_table_names()):
            return False
        return any(item.get("name") == index_name for item in inspector.get_indexes(table))

    if "evaluations" in existing_tables:
        if has_index("evaluations", "ix_eval_case_created"):
            op.drop_index("ix_eval_case_created", table_name="evaluations")
        if has_index("evaluations", op.f("ix_evaluations_final_score")):
            op.drop_index(op.f("ix_evaluations_final_score"), table_name="evaluations")
        if has_index("evaluations", op.f("ix_evaluations_created_at")):
            op.drop_index(op.f("ix_evaluations_created_at"), table_name="evaluations")
        if has_index("evaluations", op.f("ix_evaluations_case_id")):
            op.drop_index(op.f("ix_evaluations_case_id"), table_name="evaluations")
        op.drop_table("evaluations")

    if "case_capabilities" in existing_tables:
        if has_index("case_capabilities", op.f("ix_case_capabilities_case_id")):
            op.drop_index(op.f("ix_case_capabilities_case_id"), table_name="case_capabilities")
        if has_index("case_capabilities", op.f("ix_case_capabilities_capability_id")):
            op.drop_index(op.f("ix_case_capabilities_capability_id"), table_name="case_capabilities")
        op.drop_table("case_capabilities")

    if "cases" in existing_tables:
        if has_index("cases", "ix_cases_product_type_active"):
            op.drop_index("ix_cases_product_type_active", table_name="cases")
        if has_index("cases", op.f("ix_cases_title")):
            op.drop_index(op.f("ix_cases_title"), table_name="cases")
        if has_index("cases", op.f("ix_cases_slug")):
            op.drop_index(op.f("ix_cases_slug"), table_name="cases")
        if has_index("cases", op.f("ix_cases_product_type")):
            op.drop_index(op.f("ix_cases_product_type"), table_name="cases")
        if has_index("cases", op.f("ix_cases_created_at")):
            op.drop_index(op.f("ix_cases_created_at"), table_name="cases")
        if has_index("cases", op.f("ix_cases_active")):
            op.drop_index(op.f("ix_cases_active"), table_name="cases")
        op.drop_table("cases")

    if "capabilities" in existing_tables:
        if has_index("capabilities", op.f("ix_capabilities_name")):
            op.drop_index(op.f("ix_capabilities_name"), table_name="capabilities")
        if has_index("capabilities", op.f("ix_capabilities_code")):
            op.drop_index(op.f("ix_capabilities_code"), table_name="capabilities")
        if has_index("capabilities", op.f("ix_capabilities_active")):
            op.drop_index(op.f("ix_capabilities_active"), table_name="capabilities")
        op.drop_table("capabilities")
