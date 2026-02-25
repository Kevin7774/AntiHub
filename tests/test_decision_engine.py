from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from billing.db import build_session_factory
from billing.db import session_scope as billing_session_scope
from decision.db import init_decision_db
from decision.models import ProductType
from decision.repository import DecisionRepository
from decision.service import (
    FAST_MODE_NOTICE,
    recommend_products,
    resolve_product_action,
    seed_default_catalog,
)
from recommend.models import RecommendationResponse, RepoHealthCard, RepoRecommendation, RepoScoreMetric


def _health_card(score: int = 80) -> RepoHealthCard:
    return RepoHealthCard(
        overall_score=score,
        grade="A" if score >= 80 else "B",
        activity=RepoScoreMetric(label="活跃度", score=score, status="高"),
        community=RepoScoreMetric(label="社区热度", score=score, status="高"),
        maintenance=RepoScoreMetric(label="维护健康", score=score, status="高"),
        warnings=[],
        signals=[],
    )


def test_recommend_products_hybrid_ranking(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "decision_engine.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    init_decision_db(engine)

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    import decision.service as decision_service

    monkeypatch.setattr(decision_service, "session_scope", _session_scope_override)
    monkeypatch.setattr(decision_service, "RECOMMEND_EXTERNAL_SOURCES_ENABLED", False)
    seed_default_catalog()

    response = recommend_products(
        query="需要扫码支付、积分、审计日志，兼顾商户端管理和预算",
        requirement_text="",
        mode="quick",
        limit=6,
    )
    assert response.recommendations
    product_types = {str(item.product_type or "") for item in response.recommendations}
    assert "open_source" in product_types
    assert "commercial" in product_types

    top = response.recommendations[0]
    assert top.score_breakdown is not None
    expected = round(
        top.score_breakdown.relevance * 0.4
        + top.score_breakdown.popularity * 0.2
        + top.score_breakdown.cost_bonus * 0.15
        + top.score_breakdown.capability_match * 0.25
    )
    assert top.score_breakdown.final_score == expected


def test_resolve_product_action_degrades_for_commercial(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "decision_action.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    init_decision_db(engine)

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    import decision.service as decision_service

    monkeypatch.setattr(decision_service, "session_scope", _session_scope_override)
    monkeypatch.setattr(decision_service, "RECOMMEND_EXTERNAL_SOURCES_ENABLED", False)
    seed_default_catalog()

    with _session_scope_override() as session:
        repo = DecisionRepository(session)
        salesforce = repo.get_case_by_slug("salesforce")
        assert salesforce is not None
        assert salesforce.product_type == ProductType.COMMERCIAL
        salesforce_id = salesforce.id

    action = resolve_product_action(case_id=salesforce_id)
    assert action.action_type == "visit_official_site"
    assert action.deploy_supported is False
    assert action.url and action.url.startswith("https://")


def test_recommend_products_falls_back_when_ai_rerank_fails(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "decision_fallback.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    init_decision_db(engine)

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    import decision.service as decision_service

    monkeypatch.setattr(decision_service, "session_scope", _session_scope_override)
    monkeypatch.setattr(decision_service, "RECOMMEND_EXTERNAL_SOURCES_ENABLED", False)
    monkeypatch.setattr(decision_service, "llm_available", lambda: True)

    def _raise_timeout(*_args, **_kwargs):
        raise TimeoutError("llm timeout")

    monkeypatch.setattr(decision_service, "rank_candidates", _raise_timeout)

    seed_default_catalog()
    response = recommend_products(
        query="需要 CRM、支付与审计能力",
        requirement_text="",
        mode="deep",
        limit=5,
    )
    assert response.recommendations
    assert FAST_MODE_NOTICE in response.warnings


def test_recommend_products_keeps_community_query_on_topic(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "decision_community.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    init_decision_db(engine)

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    import decision.service as decision_service

    monkeypatch.setattr(decision_service, "session_scope", _session_scope_override)
    monkeypatch.setattr(decision_service, "RECOMMEND_EXTERNAL_SOURCES_ENABLED", False)
    seed_default_catalog()

    response = recommend_products(
        query="想做数字化社区，支持社群运营、话题讨论和内容审核",
        requirement_text="",
        mode="quick",
        limit=5,
    )
    assert response.recommendations
    names = [str(item.full_name or "").lower() for item in response.recommendations]
    assert any("discourse" in name or "nodebb" in name for name in names)
    assert "discourse" in names[0] or "nodebb" in names[0]


def test_recommend_products_merges_external_multi_source_when_enabled(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "decision_external_merge.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    init_decision_db(engine)

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    import decision.service as decision_service

    monkeypatch.setattr(decision_service, "session_scope", _session_scope_override)
    monkeypatch.setattr(decision_service, "RECOMMEND_EXTERNAL_SOURCES_ENABLED", True)
    seed_default_catalog()

    def _fake_external(**_kwargs) -> RecommendationResponse:
        return RecommendationResponse(
            request_id="ext-1",
            query="数字化社区",
            mode="quick",
            generated_at=0.0,
            sources=["github", "gitee", "gitcode"],
            recommendations=[
                RepoRecommendation(
                    id="github:community/awesome-forum",
                    full_name="community/awesome-forum",
                    html_url="https://github.com/community/awesome-forum",
                    description="digital community forum and moderation toolkit",
                    language="TypeScript",
                    topics=["community", "forum", "moderation"],
                    stars=2800,
                    forks=330,
                    open_issues=14,
                    license="MIT",
                    archived=False,
                    pushed_at="2026-01-01T00:00:00Z",
                    updated_days=10,
                    match_score=92,
                    match_reasons=["外部召回"],
                    match_tags=["社区"],
                    risk_notes=[],
                    health=_health_card(88),
                    source="github",
                    product_type="open_source",
                ),
                RepoRecommendation(
                    id="gitcode:corp/erp-suite",
                    full_name="corp/erp-suite",
                    html_url="https://gitcode.com/corp/erp-suite",
                    description="enterprise erp management platform",
                    language="Java",
                    topics=["erp"],
                    stars=5200,
                    forks=900,
                    open_issues=200,
                    license="Apache-2.0",
                    archived=False,
                    pushed_at="2026-01-01T00:00:00Z",
                    updated_days=8,
                    match_score=95,
                    match_reasons=["外部召回"],
                    match_tags=["ERP"],
                    risk_notes=[],
                    health=_health_card(82),
                    source="gitcode",
                    product_type="open_source",
                ),
            ],
        )

    monkeypatch.setattr(decision_service, "recommend_repositories", _fake_external)

    response = recommend_products(
        query="想做数字化社区，支持社群运营、话题讨论和内容审核",
        requirement_text="",
        mode="quick",
        limit=5,
    )
    names = [str(item.full_name or "").lower() for item in response.recommendations]
    assert any("awesome-forum" in name for name in names)
    assert "github" in response.sources


def test_recommend_products_deep_mode_contains_trace_and_citations(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "decision_deep_trace.db"
    engine, session_factory = build_session_factory(f"sqlite+pysqlite:///{db_path}")
    init_decision_db(engine)

    @contextmanager
    def _session_scope_override():
        with billing_session_scope(session_factory) as session:
            yield session

    import decision.service as decision_service

    monkeypatch.setattr(decision_service, "session_scope", _session_scope_override)
    monkeypatch.setattr(decision_service, "RECOMMEND_EXTERNAL_SOURCES_ENABLED", False)
    seed_default_catalog()

    response = recommend_products(
        query="想做数字化社区，支持社群运营、话题讨论和内容审核",
        requirement_text="",
        mode="deep",
        limit=10,
    )
    assert response.recommendations
    assert response.trace_steps
    assert response.citations
    assert response.deep_summary
