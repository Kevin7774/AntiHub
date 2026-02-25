from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

import recommend.github as github_api


def test_trim_search_query_within_budget_and_keeps_suffix() -> None:
    query = (
        "数字化社区 微信公众号 采集抓取 "
        + " ".join(f"关键词{i}" for i in range(1, 220))
        + " in:name,description,readme"
    )
    trimmed = github_api._trim_search_query(query, max_encoded_chars=220)
    assert len(quote(trimmed)) <= 220
    assert trimmed.endswith("in:name,description,readme")


def test_search_repositories_sends_trimmed_query(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def _fake_request_json(url: str, token: str | None, timeout: int = 12):  # noqa: ARG001
        captured["url"] = url
        return {"items": []}

    monkeypatch.setattr(github_api, "_request_json", _fake_request_json)

    query = (
        "数字化社区 微信公众号 采集抓取 "
        + " ".join(f"关键词{i}" for i in range(1, 260))
        + " in:name,description,readme"
    )
    items, payload = github_api.search_repositories(query, per_page=10, page=1, timeout=8)
    assert items == []
    assert isinstance(payload, dict)
    parsed = parse_qs(urlparse(captured["url"]).query)
    q = str((parsed.get("q") or [""])[0])
    assert q
    assert len(quote(q)) <= 220
