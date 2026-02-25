from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any, cast


def _load_backup_restore_module() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "backup_restore.py"
    spec = importlib.util.spec_from_file_location("backup_restore_script", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load backup_restore.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_backup_and_restore(tmp_path: Path) -> None:
    module = cast(Any, _load_backup_restore_module())
    db_path = tmp_path / "demo.db"
    url = f"sqlite:///{db_path}"

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO demo (name) VALUES ('before')")
        conn.commit()

    backup_dir = tmp_path / "backups"
    backup_file = module.backup_sqlite_database(url, backup_dir)
    assert backup_file.exists()

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM demo")
        conn.commit()

    module.restore_sqlite_database(url, backup_file)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT name FROM demo ORDER BY id ASC LIMIT 1").fetchone()
    assert row is not None
    assert str(row[0]) == "before"


def test_database_url_detection() -> None:
    module = cast(Any, _load_backup_restore_module())
    assert module._is_sqlite_url("sqlite:////tmp/a.db")
    assert module._is_postgres_url("postgresql+psycopg2://u:p@127.0.0.1:5432/db")
