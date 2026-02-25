from __future__ import annotations

from pathlib import Path

import pytest

from billing.db import build_session_factory
from billing.db import init_billing_db as billing_init_billing_db


def test_init_billing_db_requires_postgres_in_production(monkeypatch) -> None:
    import billing.db as billing_db

    monkeypatch.setattr(billing_db, "APP_ENV", "production")
    monkeypatch.setattr(billing_db, "DATABASE_URL", "sqlite:///tmp/test.db")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        billing_db.init_billing_db()


def test_init_billing_db_allows_engine_override_for_tests(tmp_path: Path) -> None:
    db_path = tmp_path / "billing_startup_test.db"
    engine, _session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    engine.dispose()
