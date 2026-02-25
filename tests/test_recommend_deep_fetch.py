from __future__ import annotations

import recommend.deep_fetch as deep_fetch


def test_fetch_repo_document_prefers_openclaw_when_available(monkeypatch) -> None:
    monkeypatch.setattr(deep_fetch, "OPENCLAW_BASE_URL", "http://127.0.0.1:8787")

    def _fake_openclaw(repo_url: str, timeout: int):  # noqa: ARG001
        return ("# README\n\ncommunity forum", "https://github.com/demo/forum", "openclaw_github_fetch", None)

    monkeypatch.setattr(deep_fetch, "_fetch_with_openclaw", _fake_openclaw)

    item = {
        "source": "github",
        "html_url": "https://github.com/demo/forum",
        "full_name": "demo/forum",
    }
    result = deep_fetch.fetch_repo_document(item, timeout=3)
    assert "community forum" in str(result.get("content") or "")
    assert result.get("fetch_source") == "openclaw_github_fetch"


def test_enrich_candidates_with_documents_sets_doc_fields(monkeypatch) -> None:
    def _fake_fetch_repo_document(item: dict, timeout: int = 10):  # noqa: ARG001
        return {
            "content": "## Features\n\n- topics\n- moderation",
            "url": item.get("html_url"),
            "fetch_source": "native_readme",
            "warnings": [],
            "duration_ms": 12,
        }

    monkeypatch.setattr(deep_fetch, "fetch_repo_document", _fake_fetch_repo_document)

    events: list[str] = []
    candidates = [
        {"id": "github:demo/forum", "full_name": "demo/forum", "html_url": "https://github.com/demo/forum"},
        {"id": "github:demo/bbs", "full_name": "demo/bbs", "html_url": "https://github.com/demo/bbs"},
    ]
    warnings = deep_fetch.enrich_candidates_with_documents(
        candidates,
        top_n=2,
        timeout=4,
        progress_callback=events.append,
    )
    assert warnings == []
    assert candidates[0].get("doc_excerpt")
    assert candidates[1].get("doc_markdown")
    assert candidates[0].get("doc_fetch_source") == "native_readme"
    assert events
