from recommend.models import RecommendationProfile
from recommend.service import (
    _build_search_queries,
    _collect_match_terms,
    _fallback_rank,
    _simple_similarity,
    recommend_repositories,
)


def test_similarity_prefers_wechat_crawler_semantics() -> None:
    query = "微信公众号情报搜奇爬取汇总"
    relevant = "wechat official-account crawler intelligence collection"
    irrelevant = "music player desktop toolkit"
    assert _simple_similarity(query, relevant) > _simple_similarity(query, irrelevant)


def test_similarity_penalizes_missing_must_groups() -> None:
    query = "微信公众号情报搜奇爬取汇总"
    full = "wechat official-account crawler intelligence collection"
    partial = "wechat official-account intelligence collection"
    assert _simple_similarity(query, full) > _simple_similarity(query, partial)


def test_collect_match_terms_contains_domain_tokens() -> None:
    query = "微信公众号情报搜奇爬取汇总"
    candidate = "best wechat crawler project for intelligence collection"
    hits = _collect_match_terms(query, candidate)
    assert any(item in hits for item in ["wechat", "crawler", "intelligence", "collection"])


def test_fallback_rank_not_dominated_by_star_count_only() -> None:
    query = "微信公众号情报搜奇爬取汇总"
    ranked = _fallback_rank(
        [
            {
                "id": "good/repo",
                "summary": "wechat crawler for official-account intelligence collection",
                "stars": 120,
                "source": "github",
            },
            {
                "id": "hot/but-irrelevant",
                "summary": "music player ui animation toolkit",
                "stars": 120000,
                "source": "github",
            },
        ],
        query=query,
        top_k=2,
    )
    assert ranked[0]["id"] == "good/repo"


def test_build_search_queries_expands_cjk_terms() -> None:
    query = "微信公众号情报搜奇爬取汇总"
    profile = RecommendationProfile(
        summary="微信情报采集与汇总",
        keywords=["微信", "爬虫", "情报"],
        must_have=["汇总"],
    )
    queries = _build_search_queries(query, query, profile)
    merged = " ".join(queries).lower()
    assert queries[0].lower().startswith("wechat ")
    assert "wechat" in merged
    assert "crawler" in merged
    assert "in:name,description,readme" in merged


def test_build_search_queries_filters_noisy_keyword_suffixes() -> None:
    query = "数字化社区 微信公众号 采集抓取 " + " ".join(f"关键词{i}" for i in range(1, 120))
    queries = _build_search_queries(query, query, None)
    merged = " ".join(queries)
    assert "关键词99" not in merged
    assert len(merged) < 320


def test_recommend_repositories_warns_when_core_semantics_missing(monkeypatch) -> None:
    query = "微信公众号情报搜奇爬取汇总"

    def fake_search_repositories(  # noqa: ARG001
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        return (
            [
                {
                    "full_name": "demo/repo",
                    "html_url": "https://github.com/demo/repo",
                    "description": "wechat official-account intelligence collection",
                    "topics": [],
                    "language": "Python",
                    "stargazers_count": 1500,
                    "forks_count": 120,
                    "open_issues_count": 3,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                }
            ],
            {"total_count": 1},
        )

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_repositories)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)

    result = recommend_repositories(query=query, requirement_text=query, mode="quick", limit=8)
    assert result.recommendations == []
    assert any("未检索到同时覆盖核心语义" in item for item in (result.warnings or []))


def test_recommend_repositories_collects_multi_source_candidates(monkeypatch) -> None:
    def fake_search_github(  # noqa: ARG001
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        return (
            [
                {
                    "full_name": "discourse/discourse",
                    "html_url": "https://github.com/discourse/discourse",
                    "description": "open source community forum platform",
                    "topics": ["community", "forum"],
                    "language": "Ruby",
                    "stargazers_count": 42000,
                    "forks_count": 8200,
                    "open_issues_count": 120,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                }
            ],
            {"total_count": 1},
        )

    def fake_search_gitee(  # noqa: ARG001
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        return (
            [
                {
                    "full_name": "mirror/nodebb",
                    "html_url": "https://gitee.com/mirror/nodebb",
                    "description": "community platform and forum",
                    "topics": ["community", "nodebb"],
                    "language": "JavaScript",
                    "stars_count": 1800,
                    "forks_count": 260,
                    "open_issues_count": 12,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                }
            ],
            {"total_count": 1},
        )

    def fake_search_gitcode(  # noqa: ARG001
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        return (
            [
                {
                    "path_with_namespace": "community/forum-kit",
                    "web_url": "https://gitcode.com/community/forum-kit",
                    "description": "digital community forum starter",
                    "tag_list": ["community", "forum"],
                    "language": "Go",
                    "star_count": 980,
                    "forks_count": 140,
                    "open_issues_count": 8,
                    "last_activity_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                }
            ],
            {"total_count": 1},
        )

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_github)
    monkeypatch.setattr("recommend.service.search_gitee_repositories", fake_search_gitee)
    monkeypatch.setattr("recommend.service.search_gitcode_repositories", fake_search_gitcode)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITEE", True)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITCODE", True)
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])

    result = recommend_repositories(
        query="数字化社区论坛系统",
        requirement_text="支持数字化社区、话题讨论与内容运营",
        mode="quick",
        limit=8,
    )
    assert result.recommendations
    assert {"github", "gitee", "gitcode"}.issubset(set(result.sources))


def test_recommend_repositories_keeps_keyword_priority_over_llm_score(monkeypatch) -> None:
    def fake_search_repositories(  # noqa: ARG001
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        return (
            [
                {
                    "full_name": "community/forum-core",
                    "html_url": "https://github.com/community/forum-core",
                    "description": "open source community forum discussion platform",
                    "topics": ["community", "forum"],
                    "language": "TypeScript",
                    "stargazers_count": 1500,
                    "forks_count": 180,
                    "open_issues_count": 20,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                },
                {
                    "full_name": "corp/erp-suite",
                    "html_url": "https://github.com/corp/erp-suite",
                    "description": "enterprise erp accounting inventory suite",
                    "topics": ["erp", "inventory"],
                    "language": "Java",
                    "stargazers_count": 12000,
                    "forks_count": 1300,
                    "open_issues_count": 200,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                },
            ],
            {"total_count": 2},
        )

    def fake_rank_candidates(_query: str, _candidates: list[dict], _top_k: int):
        # Deliberately biased model score: ERP higher than community.
        return {
            "results": [
                {"id": "github:corp/erp-suite", "score": 98, "reasons": ["semantic"], "tags": [], "risks": []},
                {
                    "id": "github:community/forum-core",
                    "score": 42,
                    "reasons": ["semantic"],
                    "tags": [],
                    "risks": [],
                },
            ]
        }

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_repositories)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])
    monkeypatch.setattr("recommend.service.llm_available", lambda: True)
    monkeypatch.setattr("recommend.service.rank_candidates", fake_rank_candidates)

    result = recommend_repositories(
        query="digital community forum",
        requirement_text="need community forum with topic discussion",
        mode="quick",
        limit=10,
    )
    assert result.recommendations
    assert result.recommendations[0].id == "github:community/forum-core"


def test_recommend_repositories_enforces_minimum_ten_results_when_pool_sufficient(monkeypatch) -> None:
    def fake_search_repositories(  # noqa: ARG001
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        items = []
        for idx in range(1, 13):
            items.append(
                {
                    "full_name": f"community/forum-{idx}",
                    "html_url": f"https://github.com/community/forum-{idx}",
                    "description": "open source community forum toolkit",
                    "topics": ["community", "forum"],
                    "language": "TypeScript",
                    "stargazers_count": 1000 + idx,
                    "forks_count": 100 + idx,
                    "open_issues_count": 10,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                }
            )
        return (items, {"total_count": len(items)})

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_repositories)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)

    result = recommend_repositories(
        query="digital community forum",
        requirement_text="need community forum with topic discussion",
        mode="quick",
        limit=1,
    )
    assert len(result.recommendations) == 10


def test_recommend_repositories_community_relaxation_avoids_empty_result(monkeypatch) -> None:
    query = "数字化社区 微信公众号 采集抓取"

    def fake_search_repositories(  # noqa: ARG001
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        return (
            [
                {
                    "full_name": "community/forum-kit",
                    "html_url": "https://github.com/community/forum-kit",
                    "description": "open source digital community forum platform",
                    "topics": ["community", "forum"],
                    "language": "TypeScript",
                    "stargazers_count": 3200,
                    "forks_count": 400,
                    "open_issues_count": 20,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                },
                {
                    "full_name": "corp/erp-suite",
                    "html_url": "https://github.com/corp/erp-suite",
                    "description": "enterprise erp inventory platform",
                    "topics": ["erp"],
                    "language": "Java",
                    "stargazers_count": 8800,
                    "forks_count": 900,
                    "open_issues_count": 120,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                },
            ],
            {"total_count": 2},
        )

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_repositories)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)

    result = recommend_repositories(query=query, requirement_text=query, mode="quick", limit=10)
    names = [str(item.full_name or "").lower() for item in result.recommendations]
    assert names
    assert any("forum-kit" in name for name in names)
    assert all("erp-suite" not in name for name in names)


def test_recommend_repositories_deep_mode_outputs_citations_and_trace(monkeypatch) -> None:
    def fake_search_repositories(  # noqa: ARG001
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        return (
            [
                {
                    "full_name": "community/forum-kit",
                    "html_url": "https://github.com/community/forum-kit",
                    "description": "open source community forum platform",
                    "topics": ["community", "forum"],
                    "language": "TypeScript",
                    "stargazers_count": 3200,
                    "forks_count": 400,
                    "open_issues_count": 20,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                }
            ],
            {"total_count": 1},
        )

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_repositories)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)
    monkeypatch.setattr("recommend.service.fetch_repo", lambda _full_name: {})

    result = recommend_repositories(
        query="数字化社区论坛系统",
        requirement_text="需要社区论坛系统",
        mode="deep",
        limit=10,
    )
    assert result.recommendations
    assert result.citations
    assert result.trace_steps
    assert result.deep_summary
    assert any("契合点" in str(item) for item in (result.insight_points or []))


def test_recommend_repositories_rewrites_long_requirement_into_technical_queries(monkeypatch) -> None:
    calls: list[str] = []

    def fake_search_repositories(
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        calls.append(_query)
        return (
            [
                {
                    "full_name": "acme/file-watch-sync",
                    "html_url": "https://github.com/acme/file-watch-sync",
                    "description": "windows file watcher incremental sync service",
                    "topics": ["filesystemwatcher", "sync", "windows-service"],
                    "language": "C#",
                    "stargazers_count": 1200,
                    "forks_count": 130,
                    "open_issues_count": 10,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                }
            ],
            {"total_count": 1},
        )

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_repositories)
    monkeypatch.setattr("recommend.service.extract_search_queries", lambda _text: ["FileSystemWatcher", "文件增量同步", "Windows后台监控服务"])
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITEE", False)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITCODE", False)
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])

    long_requirement = (
        "医院图片上传软件需要支持医生端上传、患者档案关联、异常重试。"
        "核心实现是文件夹监控触发、文件增量同步、Windows后台服务运行，"
        "并要求离线恢复与日志追踪。"
        "另外还要支持失败重传、断点续传、服务自恢复、日志审计、目录轮询降级、"
        "任务队列缓存与增量补偿策略，确保在网络抖动时稳定运行。"
    )
    result = recommend_repositories(
        query="",
        requirement_text=long_requirement,
        mode="quick",
        limit=10,
    )
    assert result.recommendations
    assert result.search_query in {"FileSystemWatcher", "文件增量同步", "Windows后台监控服务"}
    assert set(["FileSystemWatcher", "文件增量同步", "Windows后台监控服务"]).issubset(set(calls))


def test_recommend_repositories_populates_repo_url_from_html_url(monkeypatch) -> None:
    def fake_search_repositories(
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        return (
            [
                {
                    "full_name": "community/forum-kit",
                    "html_url": "https://github.com/community/forum-kit",
                    "description": "open source community forum platform",
                    "topics": ["community", "forum"],
                    "language": "TypeScript",
                    "stargazers_count": 3200,
                    "forks_count": 400,
                    "open_issues_count": 20,
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                }
            ],
            {"total_count": 1},
        )

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_repositories)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITEE", False)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITCODE", False)
    monkeypatch.setattr("recommend.service.llm_available", lambda: False)
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])

    result = recommend_repositories(
        query="digital community forum",
        requirement_text="need community forum with topic discussion",
        mode="quick",
        limit=10,
    )
    assert result.recommendations
    assert result.recommendations[0].repo_url == result.recommendations[0].html_url


def test_recommend_repositories_stops_low_precision_fallback_when_query_rewrite_unavailable(monkeypatch) -> None:
    called = {"search": False}

    def fake_search_repositories(
        _query: str,
        per_page: int = 20,
        page: int = 1,
        timeout: int = 8,
    ):
        called["search"] = True
        return ([], {"total_count": 0})

    monkeypatch.setattr("recommend.service.search_repositories", fake_search_repositories)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITEE", False)
    monkeypatch.setattr("recommend.service.RECOMMEND_ENABLE_GITCODE", False)
    monkeypatch.setattr(
        "recommend.service.extract_search_queries",
        lambda _text: (_ for _ in ()).throw(RuntimeError("深度搜索需要配置 OPENAI_API_KEY。当前长文档无法提炼技术关键词，搜索结果可能极不准确。")),
    )
    monkeypatch.setattr("recommend.service.load_templates", lambda: [])

    result = recommend_repositories(
        query="",
        requirement_text=(
            "医院医生端图片上传软件需求文档，包含 2000 字业务背景，强调文件监控、增量同步、"
            "后台服务自恢复与日志追踪。"
            "另需支持断点续传、失败重传、目录轮询降级、离线缓存、任务队列重放、"
            "端到端加密、审计追踪、告警通知、服务守护与灰度升级。"
        ),
        mode="deep",
        limit=10,
    )
    assert result.recommendations == []
    assert called["search"] is False
    assert any("OPENAI_API_KEY" in item for item in (result.warnings or []))
