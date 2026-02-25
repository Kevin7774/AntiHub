from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

import main
from visualize import service as visualize_service


class _FakeVisualizeTask:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, list[str] | None]] = []

    def delay(self, case_id: str, force: bool, kinds: list[str] | None) -> None:
        self.calls.append((case_id, force, kinds))


def test_visual_default_kinds_exclude_video() -> None:
    assert "video" not in visualize_service.DEFAULT_KINDS


def test_visualize_video_endpoint_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(main, "VISUAL_VIDEO_ENABLED", False)
    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(
            main.trigger_visualize_video(
                case_id="c_demo",
                request=None,  # type: ignore[arg-type]
                payload=main.VisualizeVideoRequest(),
            )
        )
    assert int(exc.value.status_code) == 410
    assert "disabled" in str(exc.value.detail).lower()


def test_visualize_endpoint_drops_video_kind_when_video_disabled(monkeypatch) -> None:
    @contextmanager
    def _noop_session_scope():
        yield None

    fake_task = _FakeVisualizeTask()

    monkeypatch.setattr(main, "session_scope", _noop_session_scope)
    monkeypatch.setattr(main, "VISUAL_VIDEO_ENABLED", False)
    monkeypatch.setattr(
        main,
        "get_case",
        lambda case_id: {"case_id": case_id, "repo_url": "https://example.com/repo.git", "commit_sha": "abc123"},
    )
    monkeypatch.setattr(main, "acquire_visualize_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main, "update_case", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "visualize_case", fake_task)

    response = asyncio.run(
        main.trigger_visualize(
            case_id="c_demo",
            request=None,  # type: ignore[arg-type]
            payload=main.VisualizeRequest(kinds=["video"]),
        )
    )
    assert isinstance(response, dict)
    assert response.get("visual_status") == "PENDING"
    assert fake_task.calls == [("c_demo", False, None)]
