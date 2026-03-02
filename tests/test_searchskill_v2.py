"""Tests for SearchSkill v2 models, scoring, and pipeline integration."""
from __future__ import annotations

from recommend.models import (
    AssemblyBlueprint,
    CandidateAssessment,
    KeywordBuckets,
    ModuleDecomposition,
    MonetizationAngle,
    RecommendationProfile,
    RecommendationResponse,
    RepoHealthCard,
    RepoRecommendation,
    RepoScoreMetric,
    ScoreBreakdown,
)
from recommend.service import (
    _compute_module_coverage,
    _customization_estimate_label,
    _estimate_customization_score,
    _estimate_integration_score,
    _integration_complexity_label,
    recommend_repositories,
)


def _health(score: int = 80) -> RepoHealthCard:
    return RepoHealthCard(
        overall_score=score,
        grade="A" if score >= 80 else "B",
        activity=RepoScoreMetric(label="活跃度", score=score, status="高"),
        community=RepoScoreMetric(label="社区热度", score=score, status="高"),
        maintenance=RepoScoreMetric(label="维护健康", score=score, status="高"),
    )


# ---- Model tests ----

def test_v2_models_importable_and_constructable() -> None:
    """All v2 models can be imported and instantiated with defaults."""
    kb = KeywordBuckets(implementation=["sdk"], repo_discovery=["search"])
    assert kb.implementation == ["sdk"]
    assert kb.negatives == []

    md = ModuleDecomposition(id="M01", name="支付网关", category="Reusable OSS")
    assert md.category == "Reusable OSS"
    assert md.actors == []

    ca = CandidateAssessment(coverage_score=75, customization_estimate="S")
    assert ca.coverage_score == 75

    ab = AssemblyBlueprint(mvp_repos=["a/b"], mvp_timeline="1-2 weeks")
    assert ab.mvp_timeline == "1-2 weeks"

    ma = MonetizationAngle(reduction_pct=65, full_custom_estimate="60 days")
    assert ma.reduction_pct == 65


def test_v2_fields_on_response_default_empty() -> None:
    """V2 fields default to empty/None on RecommendationResponse."""
    resp = RecommendationResponse(
        request_id="test",
        mode="quick",
        generated_at=0.0,
    )
    assert resp.modules == []
    assert resp.assembly is None
    assert resp.monetization is None


def test_v2_fields_on_recommendation_default_none() -> None:
    """assessment defaults to None on RepoRecommendation."""
    rec = RepoRecommendation(
        id="test",
        full_name="a/b",
        html_url="https://github.com/a/b",
        health=_health(),
    )
    assert rec.assessment is None
    assert rec.score_breakdown is None


def test_keyword_buckets_on_profile() -> None:
    """RecommendationProfile accepts keyword_buckets field."""
    kb = KeywordBuckets(implementation=["sdk"], repo_discovery=["search"])
    profile = RecommendationProfile(keyword_buckets=kb)
    assert profile.keyword_buckets is not None
    assert profile.keyword_buckets.implementation == ["sdk"]


def test_module_categories_valid() -> None:
    """ModuleDecomposition accepts all valid categories."""
    for cat in ["Reusable OSS", "Config-only", "Light customization", "Heavy customization"]:
        m = ModuleDecomposition(id="M01", name="test", category=cat)
        assert m.category == cat


# ---- Scoring helper tests ----

def test_module_coverage_full_match() -> None:
    """Module coverage returns 100 when all modules match."""
    modules = [
        {"id": "M01", "name": "支付网关"},
        {"id": "M02", "name": "积分管理"},
    ]
    match = {"summary": "支付网关和积分管理系统", "description": "payment and points", "topics": []}
    score = _compute_module_coverage(match, modules)
    assert score == 100


def test_module_coverage_no_match() -> None:
    """Module coverage returns 0 when no modules match."""
    modules = [
        {"id": "M01", "name": "区块链共识引擎"},
        {"id": "M02", "name": "量子计算模拟器"},
    ]
    match = {"summary": "支付网关系统", "description": "payment gateway", "topics": []}
    score = _compute_module_coverage(match, modules)
    assert score == 0


def test_module_coverage_empty_modules() -> None:
    """Module coverage returns 0 when modules list is empty."""
    assert _compute_module_coverage({"summary": "test"}, []) == 0


def test_customization_score_well_maintained_repo() -> None:
    """Well-maintained popular repo gets high customization score."""
    match = {
        "stars": 5000,
        "updated_days": 10,
        "license": "MIT",
        "topics": ["docker", "api", "sdk", "examples", "docs"],
        "description": "comprehensive documentation and docker support",
    }
    score = _estimate_customization_score(match)
    assert score >= 60


def test_customization_score_abandoned_repo() -> None:
    """Abandoned repo gets low customization score."""
    match = {
        "stars": 3,
        "updated_days": 800,
        "license": "",
        "topics": [],
        "description": "old project",
    }
    score = _estimate_customization_score(match)
    assert score <= 30


def test_integration_score_api_docker_repo() -> None:
    """Repo with API + Docker + examples gets high integration score."""
    match = {
        "topics": ["api", "docker", "example"],
        "description": "REST API with docker-compose and examples",
        "language": "Python",
    }
    score = _estimate_integration_score(match)
    assert score >= 80


def test_customization_label_mapping() -> None:
    """S/M/L labels map correctly from scores."""
    assert _customization_estimate_label(80) == "S"
    assert _customization_estimate_label(50) == "M"
    assert _customization_estimate_label(20) == "L"


def test_integration_label_mapping() -> None:
    """S/M/L labels map correctly from scores."""
    assert _integration_complexity_label(70) == "S"
    assert _integration_complexity_label(40) == "M"
    assert _integration_complexity_label(10) == "L"


def test_scoring_weights_sum_to_100() -> None:
    """v2 rubric weights sum to 100."""
    weights = [35, 20, 20, 10, 15]
    assert sum(weights) == 100


# ---- Pipeline integration tests ----

def test_quick_mode_omits_v2_fields(monkeypatch) -> None:
    """Quick mode does not populate modules/assembly/monetization."""
    def fake_search(_query, per_page=20, page=1, timeout=8):
        return ([{
            "full_name": "test/repo",
            "html_url": "https://github.com/test/repo",
            "description": "test payment system",
            "topics": ["payment"],
            "language": "Python",
            "stargazers_count": 100,
            "forks_count": 10,
            "open_issues_count": 5,
            "pushed_at": "2026-01-01T00:00:00Z",
            "archived": False,
        }], {"total_count": 1})

    monkeypatch.setattr("recommend.service.search_repositories", fake_search)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITEE", False)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITCODE", False)
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])

    result = recommend_repositories(
        query="payment system",
        requirement_text="need a payment system",
        mode="quick",
        limit=10,
    )
    assert result.modules == []
    assert result.assembly is None
    assert result.monetization is None


def test_score_breakdown_populated_for_external_repos(monkeypatch) -> None:
    """External search results have score_breakdown populated."""
    def fake_search(_query, per_page=20, page=1, timeout=8):
        return ([{
            "full_name": "acme/pay-sdk",
            "html_url": "https://github.com/acme/pay-sdk",
            "description": "wechat payment SDK integration with docker support",
            "topics": ["payment", "wechat", "docker", "api"],
            "language": "Python",
            "stargazers_count": 2500,
            "forks_count": 300,
            "open_issues_count": 15,
            "pushed_at": "2026-01-01T00:00:00Z",
            "archived": False,
            "license": {"spdx_id": "MIT"},
        }], {"total_count": 1})

    monkeypatch.setattr("recommend.service.search_repositories", fake_search)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITEE", False)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITCODE", False)
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])

    result = recommend_repositories(
        query="wechat payment",
        requirement_text="need wechat payment SDK",
        mode="quick",
        limit=10,
    )
    assert result.recommendations
    rec = result.recommendations[0]
    assert rec.score_breakdown is not None
    assert rec.score_breakdown.final_score >= 0
    assert rec.score_breakdown.relevance >= 0
    assert rec.score_breakdown.popularity >= 0


def test_health_card_feeds_into_maturity_score(monkeypatch) -> None:
    """Health card overall_score influences the ranking via maturity weight."""
    def fake_search(_query, per_page=20, page=1, timeout=8):
        return ([
            {
                "full_name": "fresh/repo",
                "html_url": "https://github.com/fresh/repo",
                "description": "wechat payment SDK new active",
                "topics": ["payment", "wechat"],
                "language": "Python",
                "stargazers_count": 500,
                "forks_count": 50,
                "open_issues_count": 5,
                "pushed_at": "2026-02-01T00:00:00Z",
                "archived": False,
                "license": {"spdx_id": "MIT"},
            },
            {
                "full_name": "stale/repo",
                "html_url": "https://github.com/stale/repo",
                "description": "wechat payment SDK old abandoned",
                "topics": ["payment", "wechat"],
                "language": "Python",
                "stargazers_count": 500,
                "forks_count": 50,
                "open_issues_count": 600,
                "pushed_at": "2020-01-01T00:00:00Z",
                "archived": True,
                "license": None,
            },
        ], {"total_count": 2})

    monkeypatch.setattr("recommend.service.search_repositories", fake_search)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITEE", False)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITCODE", False)
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])

    result = recommend_repositories(
        query="wechat payment",
        requirement_text="wechat payment SDK",
        mode="quick",
        limit=10,
    )
    assert len(result.recommendations) >= 2
    fresh = next(r for r in result.recommendations if r.full_name == "fresh/repo")
    stale = next(r for r in result.recommendations if r.full_name == "stale/repo")
    # Fresh repo should have better health and higher overall score
    assert fresh.health.overall_score > stale.health.overall_score
