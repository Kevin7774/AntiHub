#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from alembic import command

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from auth import hash_password_bcrypt
from billing import BillingRepository, session_scope
from billing.models import Base as BillingBase
from config import DATABASE_URL
from decision.models import DecisionBase
from decision.service import seed_default_catalog


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _wait_for_postgres(database_url: str) -> None:
    max_attempts = _env_int("INIT_DB_MAX_ATTEMPTS", 30)
    sleep_seconds = _env_int("INIT_DB_SLEEP_SECONDS", 2)
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                print(f"[init-prod-db] postgres reachable (attempt={attempt})")
                return
            except Exception as exc:  # noqa: BLE001
                print(f"[init-prod-db] waiting for postgres (attempt={attempt}/{max_attempts}): {exc}")
                time.sleep(max(1, sleep_seconds))
        raise RuntimeError("postgres is not reachable after retries")
    finally:
        engine.dispose()


def _run_alembic_migrations(database_url: str) -> None:
    cfg = AlembicConfig(str(ROOT_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")
    print("[init-prod-db] alembic upgrade head completed")


def _fallback_create_all(database_url: str) -> None:
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        BillingBase.metadata.create_all(bind=engine)
        DecisionBase.metadata.create_all(bind=engine)
        _fallback_reconcile_billing_schema(engine)
        print("[init-prod-db] fallback create_all completed")
    finally:
        engine.dispose()


def _fallback_reconcile_billing_schema(engine) -> None:
    """
    Best-effort schema reconciliation for environments that skip Alembic.

    The fallback path should not block startup; SQL failures are logged and ignored.
    """

    statements = [
        "ALTER TABLE billing_plans ADD COLUMN IF NOT EXISTS billing_cycle VARCHAR(16)",
        "ALTER TABLE billing_plans ADD COLUMN IF NOT EXISTS trial_days INTEGER",
        "ALTER TABLE billing_plans ADD COLUMN IF NOT EXISTS metadata JSONB",
        (
            "CREATE TABLE IF NOT EXISTS billing_plan_entitlements ("
            "id VARCHAR(36) PRIMARY KEY, "
            "plan_id VARCHAR(36) NOT NULL REFERENCES billing_plans(id), "
            "key VARCHAR(128) NOT NULL, "
            "enabled BOOLEAN NOT NULL DEFAULT TRUE, "
            "value JSONB NULL, "
            "limit_value INTEGER NULL, "
            "metadata JSONB NULL, "
            "created_at TIMESTAMPTZ NOT NULL, "
            "updated_at TIMESTAMPTZ NOT NULL"
            ")"
        ),
        "CREATE INDEX IF NOT EXISTS ix_billing_plan_entitlements_plan_id ON billing_plan_entitlements(plan_id)",
        "CREATE INDEX IF NOT EXISTS ix_billing_plan_entitlements_key ON billing_plan_entitlements(key)",
        "CREATE INDEX IF NOT EXISTS ix_billing_plan_entitlements_enabled ON billing_plan_entitlements(enabled)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_billing_plan_entitlements_plan_key ON billing_plan_entitlements(plan_id, key)",
    ]
    with engine.begin() as conn:
        for sql in statements:
            try:
                conn.execute(text(sql))
            except Exception as exc:  # noqa: BLE001
                print(f"[init-prod-db] fallback reconcile skipped: {exc}")


def _bootstrap_admin_user() -> None:
    username = str(os.getenv("PROD_ADMIN_USERNAME", "admin")).strip() or "admin"
    role = str(os.getenv("PROD_ADMIN_ROLE", "admin")).strip().lower() or "admin"
    if role not in {"admin", "user", "root"}:
        role = "user"

    bcrypt_hash = str(os.getenv("PROD_ADMIN_PASSWORD_HASH_BCRYPT", "")).strip()
    password_plain = str(os.getenv("PROD_ADMIN_PASSWORD", "")).strip()

    if not bcrypt_hash and not password_plain:
        print("[init-prod-db] skip admin bootstrap: PROD_ADMIN_PASSWORD(_HASH_BCRYPT) missing")
        return

    password_hash = bcrypt_hash or hash_password_bcrypt(password_plain)
    with session_scope() as session:
        repo = BillingRepository(session)
        repo.upsert_auth_user(
            username=username,
            password_hash=password_hash,
            role=role,
            active=True,
        )
    print(f"[init-prod-db] admin user ensured: {username} ({role})")


def _bootstrap_root_user() -> None:
    username = str(os.getenv("ROOT_ADMIN_USERNAME", "root")).strip() or "root"
    password_hash = str(os.getenv("ROOT_ADMIN_PASSWORD_HASH", "")).strip()
    password_plain = str(os.getenv("ROOT_ADMIN_PASSWORD", "")).strip()
    if not password_hash and not password_plain:
        print("[init-prod-db] skip root bootstrap: ROOT_ADMIN_PASSWORD(_HASH) missing")
        return
    resolved_hash = password_hash or hash_password_bcrypt(password_plain)
    with session_scope() as session:
        repo = BillingRepository(session)
        repo.upsert_auth_user(
            username=username,
            password_hash=resolved_hash,
            role="root",
            active=True,
            tenant_id="",
        )
    print(f"[init-prod-db] root user ensured: {username} (root)")


def main() -> int:
    database_url = str(DATABASE_URL or os.getenv("DATABASE_URL", "")).strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing")
    if not database_url.lower().startswith("postgresql"):
        raise RuntimeError("DATABASE_URL must be PostgreSQL in production")

    _wait_for_postgres(database_url)

    try:
        _run_alembic_migrations(database_url)
    except Exception as exc:  # noqa: BLE001
        print(f"[init-prod-db] alembic failed, fallback to create_all: {exc}")
        _fallback_create_all(database_url)

    _bootstrap_root_user()
    _bootstrap_admin_user()
    seed_default_catalog()
    print("[init-prod-db] default catalog ensured")
    print("[init-prod-db] initialization completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
