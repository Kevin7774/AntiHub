from __future__ import annotations

import asyncio
import json

import main
from recommend.models import RecommendationResponse


def test_recommendation_stream_returns_thoughts_and_result(monkeypatch) -> None:
    def _fake_recommend_products(**_kwargs):
        return RecommendationResponse(
            request_id="req_stream",
            query="CRM",
            mode="quick",
            generated_at=0.0,
            warnings=["AI 服务繁忙，已切换至极速模式（关键词检索）。"],
            recommendations=[],
        )

    monkeypatch.setattr(main, "recommend_products", _fake_recommend_products)

    response = asyncio.run(
        main.recommend_repos_stream(
            query="CRM",
            mode="quick",
            limit=8,
            file=None,
        )
    )
    assert str(response.media_type) == "application/x-ndjson"

    async def _collect() -> str:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunks.append(chunk.decode("utf-8", errors="replace"))
            else:
                chunks.append(str(chunk))
        return "".join(chunks)

    body = asyncio.run(_collect())
    lines = [line for line in body.splitlines() if line.strip()]
    assert lines
    events = [json.loads(line) for line in lines]
    assert any(item.get("type") == "thought" for item in events)
    assert any("极速模式" in str(item.get("message", "")) for item in events if item.get("type") == "thought")
    result = next((item for item in events if item.get("type") == "result"), None)
    assert result is not None
    assert result.get("data", {}).get("request_id") == "req_stream"


def test_prepare_recommendation_input_enforces_min_limit_of_ten() -> None:
    query_value, mode_value, limit_value, requirement_text, warnings = asyncio.run(
        main._prepare_recommendation_input(
            query="CRM",
            mode="quick",
            limit=1,
            file=None,
        )
    )
    assert query_value == "CRM"
    assert mode_value == "quick"
    assert requirement_text == ""
    assert warnings == []
    assert limit_value == 10
