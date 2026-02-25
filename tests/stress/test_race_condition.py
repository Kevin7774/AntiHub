from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from billing import (
    BillingRepository,
    BillingStateError,
    PointFlowType,
    build_session_factory,
    init_billing_db,
    session_scope,
)


def test_simultaneous_point_deduction_never_goes_negative(tmp_path: Path) -> None:
    db_path = tmp_path / "race_condition.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    init_billing_db(engine)

    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        repo.record_point_flow(
            user_id="race-user",
            flow_type=PointFlowType.GRANT,
            points=100,
            idempotency_key="seed-grant",
            note="seed points",
        )

    def _consume_once(index: int) -> bool:
        try:
            with session_scope(session_factory) as session:
                repo = BillingRepository(session)
                repo.consume_points(
                    user_id="race-user",
                    points=10,
                    idempotency_key=f"consume-{index}",
                    note="stress consume",
                )
            return True
        except BillingStateError:
            return False

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(_consume_once, range(20)))

    success_count = sum(1 for item in results if item)
    with session_scope(session_factory) as session:
        repo = BillingRepository(session)
        final_balance = repo.get_user_point_balance("race-user")

    assert success_count <= 10
    assert final_balance >= 0
    assert final_balance == 100 - (success_count * 10)

    engine.dispose()
