from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from config import APP_ENV, DATABASE_ECHO, DATABASE_URL, ROOT_DIR

from .models import Base

SessionFactory = Callable[[], Session]


def _ensure_sqlite_parent(url: str) -> None:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    db_path = url[len(prefix):].split("?", 1)[0]
    if not db_path or db_path == ":memory:":
        return
    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)


def _build_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {
        "future": True,
        "echo": DATABASE_ECHO,
        "pool_pre_ping": True,
    }
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def build_session_factory(database_url: str) -> tuple[Engine, SessionFactory]:
    _ensure_sqlite_parent(database_url)
    engine = _build_engine(database_url)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
    return engine, session_factory


ENGINE, SessionLocal = build_session_factory(DATABASE_URL)


def _is_production_env() -> bool:
    return str(APP_ENV or "").strip().lower() in {"prod", "production"}


def _is_postgres_url(database_url: str) -> bool:
    normalized = str(database_url or "").strip().lower()
    return normalized.startswith("postgresql://") or normalized.startswith("postgresql+")


def _run_alembic_upgrade(revision: str = "head") -> None:
    config_path = Path(ROOT_DIR).resolve() / "alembic.ini"
    if not config_path.exists():
        raise RuntimeError(f"missing alembic.ini: {config_path}")
    alembic_cfg = AlembicConfig(str(config_path))
    alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(alembic_cfg, revision)


def init_billing_db(engine: Engine | None = None) -> None:
    if engine is not None:
        # Test-only fast path: isolated engines can still be initialized directly.
        Base.metadata.create_all(bind=engine)
        return
    if _is_production_env() and not _is_postgres_url(DATABASE_URL):
        raise RuntimeError("DATABASE_URL must be PostgreSQL in production")
    _run_alembic_upgrade("head")


@contextmanager
def session_scope(session_factory: SessionFactory | None = None) -> Iterator[Session]:
    factory = session_factory or SessionLocal
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
