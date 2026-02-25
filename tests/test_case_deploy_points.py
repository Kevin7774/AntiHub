from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

import main
import storage
from billing import PointFlowType
from billing.db import build_session_factory
from billing.db import init_billing_db as billing_init_billing_db
from billing.db import session_scope as billing_session_scope


def _apply_test_overrides(monkeypatch, session_factory, engine) -> None:
    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    monkeypatch.setattr(main, "session_scope", _session_scope_override)
    monkeypatch.setattr(main, "init_billing_db", lambda: billing_init_billing_db(engine))
    monkeypatch.setattr(main, "init_decision_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_catalog", lambda: None)
    monkeypatch.setattr(main, "ONE_CLICK_DEPLOY_POINTS_COST", 2000)
    monkeypatch.setattr(main.build_and_run, "delay", lambda *args, **kwargs: None)

    monkeypatch.setattr(storage, "USE_MEMORY_STORE", True)
    monkeypatch.setattr(storage, "USE_DB_CASE_STORE", False)
    storage._MEM_CASES.clear()
    storage._MEM_LOGS.clear()
    storage._MEM_MANUAL.clear()
    storage._MEM_MANUAL_META.clear()
    storage._MEM_MANUAL_STATUS.clear()
    storage._MEM_STATS.clear()


def test_one_click_deploy_consumes_points(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "case_deploy_points.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_test_overrides(monkeypatch, session_factory, engine)

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        repo.record_point_flow(
            user_id="system",
            flow_type=PointFlowType.GRANT,
            points=3000,
            idempotency_key="seed-points-system",
            note="seed",
        )

    payload = main.CaseCreateRequest(
        repo_url="https://example.com/demo.git",
        run_mode="container",
        one_click_deploy=True,
    )
    response = asyncio.run(main.create_case(payload))
    assert response.one_click_deploy is True
    assert int(response.deploy_points_cost or 0) == 2000

    with billing_session_scope(session_factory) as session:
        repo = main.BillingRepository(session)
        assert repo.get_user_point_balance("system") == 1000

    engine.dispose()


def test_one_click_deploy_rejects_when_points_insufficient(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "case_deploy_points_insufficient.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    billing_init_billing_db(engine)
    _apply_test_overrides(monkeypatch, session_factory, engine)

    payload = main.CaseCreateRequest(
        repo_url="https://example.com/demo.git",
        run_mode="container",
        one_click_deploy=True,
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.create_case(payload))
    assert exc_info.value.status_code == 402
    assert "积分不足" in str(exc_info.value.detail)

    engine.dispose()
