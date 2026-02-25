from typing import List, Optional

from pydantic import BaseModel, Field


class RepoScoreMetric(BaseModel):
    label: str
    score: int
    status: Optional[str] = None
    value: Optional[str] = None
    detail: Optional[str] = None


class RepoHealthCard(BaseModel):
    overall_score: int
    grade: str
    activity: RepoScoreMetric
    community: RepoScoreMetric
    maintenance: RepoScoreMetric
    warnings: List[str] = Field(default_factory=list)
    signals: List[str] = Field(default_factory=list)


class CapabilityTag(BaseModel):
    code: str
    name: str
    weight: int = 100


class ScoreBreakdown(BaseModel):
    relevance: int
    popularity: int
    cost_bonus: int
    capability_match: int
    final_score: int


class RecommendationAction(BaseModel):
    action_type: str
    label: str
    url: Optional[str] = None
    deploy_supported: bool = False
    detail: Optional[str] = None


class RepoRecommendation(BaseModel):
    id: str
    full_name: str
    html_url: str
    description: Optional[str] = None
    language: Optional[str] = None
    topics: List[str] = Field(default_factory=list)
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    license: Optional[str] = None
    archived: Optional[bool] = None
    pushed_at: Optional[str] = None
    updated_days: Optional[int] = None
    match_score: int = 0
    match_reasons: List[str] = Field(default_factory=list)
    match_tags: List[str] = Field(default_factory=list)
    risk_notes: List[str] = Field(default_factory=list)
    health: RepoHealthCard
    source: Optional[str] = None
    product_type: Optional[str] = None
    official_url: Optional[str] = None
    repo_url: Optional[str] = None
    capabilities: List[CapabilityTag] = Field(default_factory=list)
    score_breakdown: Optional[ScoreBreakdown] = None
    action: Optional[RecommendationAction] = None
    deployment_mode: Optional[str] = None


class RecommendationProfile(BaseModel):
    summary: Optional[str] = None
    search_query: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    must_have: List[str] = Field(default_factory=list)
    nice_to_have: List[str] = Field(default_factory=list)
    target_stack: List[str] = Field(default_factory=list)
    scenarios: List[str] = Field(default_factory=list)


class RecommendationCitation(BaseModel):
    id: str
    source: str
    title: str
    url: str
    snippet: Optional[str] = None
    score: Optional[int] = None
    reason: Optional[str] = None


class RecommendationResponse(BaseModel):
    request_id: str
    query: Optional[str] = None
    mode: str
    generated_at: float
    requirement_excerpt: Optional[str] = None
    search_query: Optional[str] = None
    profile: Optional[RecommendationProfile] = None
    warnings: List[str] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    deep_summary: Optional[str] = None
    insight_points: List[str] = Field(default_factory=list)
    trace_steps: List[str] = Field(default_factory=list)
    citations: List[RecommendationCitation] = Field(default_factory=list)
    recommendations: List[RepoRecommendation] = Field(default_factory=list)
