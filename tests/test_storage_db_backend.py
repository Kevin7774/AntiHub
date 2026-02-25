from __future__ import annotations

import importlib
from pathlib import Path


def _reload_storage_with_db_backend(monkeypatch, tmp_path: Path, *, log_retention: int = 200):
    db_path = tmp_path / "runtime_store.db"
    monkeypatch.setenv("CASE_STORE_BACKEND", "database")
    monkeypatch.setenv("CASE_STORE_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("REDIS_DISABLED", "true")
    monkeypatch.setenv("LOG_RETENTION_LINES", str(int(log_retention)))

    import config as config_module
    import storage as storage_module

    importlib.reload(config_module)
    return importlib.reload(storage_module)


def test_db_case_store_round_trip(monkeypatch, tmp_path: Path) -> None:
    storage = _reload_storage_with_db_backend(monkeypatch, tmp_path)

    storage.set_case("c_demo", {"status": "PENDING", "stage": "system"})
    storage.update_case("c_demo", {"status": "RUNNING"})

    payload = storage.get_case("c_demo")
    assert payload is not None
    assert payload.get("status") == "RUNNING"
    assert payload.get("updated_at") is not None

    ids = storage.list_case_ids()
    assert "c_demo" in ids


def test_db_log_and_manual_store(monkeypatch, tmp_path: Path) -> None:
    storage = _reload_storage_with_db_backend(monkeypatch, tmp_path)

    storage.append_log("c_demo", {"stream": "system", "line": "first"})
    storage.append_log("c_demo", {"stream": "system", "line": "second"})
    logs = storage.get_logs("c_demo")
    assert len(logs) == 2
    assert logs[0].get("line") == "first"
    assert logs[1].get("line") == "second"

    storage.set_manual("c_demo", "# Manual", {"generated_at": 1.0})
    markdown, meta = storage.get_manual("c_demo")
    assert markdown == "# Manual"
    assert isinstance(meta, dict)
    assert float(meta.get("generated_at") or 0) == 1.0

    storage.set_manual_status("c_demo", "SUCCESS", generated_at=2.0)
    status = storage.get_manual_status("c_demo")
    assert status is not None
    assert status.get("status") == "SUCCESS"

    storage.record_manual_stats(120, True)
    storage.record_manual_stats(180, False)
    stats = storage.get_manual_stats()
    assert int(stats.get("manual_generation_count") or 0) == 2
    assert int(stats.get("manual_generation_success") or 0) == 1
    assert int(stats.get("manual_generation_failures") or 0) == 1
    assert int(stats.get("manual_generation_latency_avg_ms") or 0) == 150


def test_db_log_retention(monkeypatch, tmp_path: Path) -> None:
    storage = _reload_storage_with_db_backend(monkeypatch, tmp_path, log_retention=2)
    storage.append_log("c_keep", {"stream": "system", "line": "l1"})
    storage.append_log("c_keep", {"stream": "system", "line": "l2"})
    storage.append_log("c_keep", {"stream": "system", "line": "l3"})

    logs = storage.get_logs("c_keep")
    assert [entry.get("line") for entry in logs] == ["l2", "l3"]
