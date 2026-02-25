from __future__ import annotations

import json

import worker


class _Request:
    def __init__(self, retries: int) -> None:
        self.retries = retries


class _Sender:
    def __init__(self, *, name: str, max_retries: int, retries: int) -> None:
        self.name = name
        self.max_retries = max_retries
        self.request = _Request(retries)


def test_task_failure_moves_exhausted_job_to_dead_letter(monkeypatch) -> None:
    memory = worker._MemoryRedis()
    monkeypatch.setattr(worker, "redis_client", memory)
    monkeypatch.setattr(worker, "WORKER_DEAD_LETTER_KEY", "test:dead_letters")

    sender = _Sender(name="analyze_case", max_retries=2, retries=2)
    worker._handle_task_failure(
        sender=sender,
        task_id="task-1",
        exception=RuntimeError("boom"),
        args=("case-1",),
        kwargs={"force": False},
    )
    rows = memory.lrange("test:dead_letters", 0, -1)
    assert rows
    payload = json.loads(rows[-1])
    assert payload["task"] == "analyze_case"
    assert payload["task_id"] == "task-1"
    assert payload["retries"] == 2


def test_task_failure_before_max_retry_does_not_dead_letter(monkeypatch) -> None:
    memory = worker._MemoryRedis()
    monkeypatch.setattr(worker, "redis_client", memory)
    monkeypatch.setattr(worker, "WORKER_DEAD_LETTER_KEY", "test:dead_letters")

    sender = _Sender(name="visualize_case", max_retries=3, retries=1)
    worker._handle_task_failure(
        sender=sender,
        task_id="task-2",
        exception=RuntimeError("temporary"),
        args=("case-2",),
        kwargs={"force": True},
    )
    assert memory.lrange("test:dead_letters", 0, -1) == []
