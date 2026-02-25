from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from billing.db import ENGINE, SessionLocal
from config import APP_ENV

from .models import DecisionBase


def _is_production_env() -> bool:
    return str(APP_ENV or "").strip().lower() in {"prod", "production"}


def init_decision_db(engine: Engine | None = None) -> None:
    if engine is None and _is_production_env():
        # Production startup uses Alembic migrations via billing.init_billing_db().
        return
    DecisionBase.metadata.create_all(bind=engine or ENGINE)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
