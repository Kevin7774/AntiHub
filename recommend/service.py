import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from analyze.signals import sanitize_text
from config import (
    RECOMMEND_DEEP_DOC_FETCH_LIMIT,
    RECOMMEND_DEEP_DOC_TIMEOUT_SECONDS,
    RECOMMEND_ENABLE_GITCODE,
    RECOMMEND_ENABLE_GITEE,
    RECOMMEND_GITHUB_MAX_RESULTS,
    RECOMMEND_MAX_TEXT_CHARS,
    RECOMMEND_PROVIDER_TIMEOUT_SECONDS,
    RECOMMEND_TOP_K,
)
from recommend.gitcode import GitCodeAPIError
from recommend.gitcode import search_repositories as search_gitcode_repositories
from recommend.deep_fetch import enrich_candidates_with_documents
from recommend.gitee import GiteeAPIError
from recommend.gitee import search_repositories as search_gitee_repositories
from recommend.github import GitHubAPIError, fetch_repo, search_repositories
from recommend.llm import (
    build_requirement_profile,
    extract_search_queries,
    llm_available,
    rank_candidates,
    summarize_findings,
)
from recommend.models import (
    RecommendationCitation,
    RecommendationProfile,
    RecommendationResponse,
    RepoHealthCard,
    RepoRecommendation,
    RepoScoreMetric,
)
from templates_store import load_templates

CJK_SYNONYM_MAP: Dict[str, List[str]] = {
    "微信": ["wechat", "weixin", "mp-weixin"],
    "公众号": ["official-account", "wechat-official-account"],
    "爬虫": ["crawler", "scraper", "spider"],
    "爬取": ["crawler", "scraper", "spider"],
    "抓取": ["crawler", "scraper", "spider"],
    "情报": ["intel", "intelligence", "threat"],
    "汇总": ["collection", "aggregator", "awesome"],
    "聚合": ["aggregator", "collection"],
}

SEMANTIC_GROUPS: Dict[str, List[str]] = {
    "wechat": ["微信", "公众号", "wechat", "weixin", "official-account", "mp-weixin"],
    "crawl": ["爬虫", "爬取", "抓取", "crawler", "scraper", "spider"],
    "intel": ["情报", "intel", "intelligence", "threat"],
    "aggregate": ["汇总", "聚合", "collection", "aggregator", "awesome"],
}

SEMANTIC_GROUP_LABELS: Dict[str, str] = {
    "wechat": "微信生态",
    "crawl": "采集抓取",
    "intel": "情报分析",
    "aggregate": "汇总聚合",
}

COMMUNITY_QUERY_ALIASES = ["community", "forum", "bbs", "社区", "社群", "论坛", "数字化社区"]

EN_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "into",
    "your",
    "their",
    "project",
    "repository",
}

CN_STOPWORDS = {
    "这个",
    "那个",
    "我们",
    "你们",
    "他们",
    "以及",
    "用于",
    "进行",
    "相关",
    "项目",
    "仓库",
    "需求",
}


def is_deep_search_mode(mode: str) -> bool:
    return str(mode or "").strip().lower() == "deep"


def _is_noise_term(term: str) -> bool:
    token = str(term or "").strip().lower()
    if not token:
        return True
    if token.isdigit():
        return True
    if re.fullmatch(r"(keyword|kw)\d{1,5}", token):
        return True
    if re.fullmatch(r"关键词\d{1,5}", token):
        return True
    return False


def _now_ts() -> float:
    return time.time()


def _to_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return int(default)
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _parse_full_name(repo_url: str) -> Optional[str]:
    if not repo_url:
        return None
    cleaned = repo_url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -4]
    match = re.search(r"github.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)$", cleaned)
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def _normalize_topics(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _normalize_license(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        spdx = value.get("spdx_id")
        if spdx and spdx != "NOASSERTION":
            return str(spdx)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _days_since(iso_time: Optional[str]) -> Optional[int]:
    if not iso_time:
        return None
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    delta = now - dt
    return max(0, int(delta.total_seconds() // 86400))


def _score_status(score: int) -> str:
    if score >= 80:
        return "高"
    if score >= 60:
        return "中"
    return "低"


def _compute_health_card(repo: Dict[str, Any]) -> RepoHealthCard:
    stars = int(repo.get("stars") or 0)
    forks = int(repo.get("forks") or 0)
    open_issues = int(repo.get("open_issues") or 0)
    archived = bool(repo.get("archived"))
    updated_days = repo.get("updated_days")
    license_name = repo.get("license")

    if updated_days is None:
        activity_score = 60
    elif updated_days <= 30:
        activity_score = 95
    elif updated_days <= 90:
        activity_score = 85
    elif updated_days <= 180:
        activity_score = 70
    elif updated_days <= 365:
        activity_score = 55
    else:
        activity_score = 35

    if archived:
        activity_score = min(activity_score, 20)

    star_score = min(100.0, math.log10(stars + 1) * 25.0)
    fork_score = min(60.0, math.log10(forks + 1) * 15.0)
    community_score = int(min(100.0, star_score + fork_score))

    maintenance_score = 100
    if archived:
        maintenance_score = 30
    if open_issues > 500:
        maintenance_score -= 40
    elif open_issues > 200:
        maintenance_score -= 25
    elif open_issues > 50:
        maintenance_score -= 10
    if not license_name:
        maintenance_score -= 15
    maintenance_score = max(10, min(100, maintenance_score))

    overall = int(round(activity_score * 0.4 + community_score * 0.35 + maintenance_score * 0.25))
    if overall >= 80:
        grade = "A"
    elif overall >= 60:
        grade = "B"
    else:
        grade = "C"

    warnings: List[str] = []
    signals: List[str] = []
    if archived:
        warnings.append("仓库已归档")
    if updated_days is not None:
        signals.append(f"最近更新 {updated_days} 天前")
        if updated_days > 365:
            warnings.append("更新周期较长")
    if not license_name:
        warnings.append("许可证未明确")
    if open_issues > 200:
        warnings.append("未处理 Issue 较多")

    signals.append(f"Stars {stars}")
    signals.append(f"Forks {forks}")
    if license_name:
        signals.append(f"License {license_name}")

    return RepoHealthCard(
        overall_score=overall,
        grade=grade,
        activity=RepoScoreMetric(
            label="活跃度",
            score=activity_score,
            status=_score_status(activity_score),
            value=f"{updated_days} 天前" if updated_days is not None else "未知",
        ),
        community=RepoScoreMetric(
            label="社区热度",
            score=community_score,
            status=_score_status(community_score),
            value=f"{stars} stars",
        ),
        maintenance=RepoScoreMetric(
            label="维护健康",
            score=maintenance_score,
            status=_score_status(maintenance_score),
            value=f"{open_issues} issues",
        ),
        warnings=warnings,
        signals=signals,
    )


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for raw in items:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _extract_ascii_terms(text: str) -> List[str]:
    terms: List[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9_+#\.-]{1,31}", (text or "").lower()):
        if token in EN_STOPWORDS:
            continue
        if len(token) < 2:
            continue
        terms.append(token)
    return _dedupe_keep_order(terms)


def _extract_cjk_terms(text: str) -> List[str]:
    terms: List[str] = []
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    for chunk in chunks:
        normalized = chunk.strip()
        if not normalized:
            continue
        if len(normalized) <= 14:
            terms.append(normalized)
        matched_keyword = False
        for keyword in CJK_SYNONYM_MAP.keys():
            if keyword in normalized:
                terms.append(keyword)
                matched_keyword = True
        if not matched_keyword and len(normalized) >= 4:
            terms.append(normalized[:2])
            terms.append(normalized[-2:])
    return _dedupe_keep_order([term for term in terms if term not in CN_STOPWORDS])


def _query_terms(text: str) -> List[str]:
    raw = sanitize_text(text or "")
    terms = _extract_ascii_terms(raw) + _extract_cjk_terms(raw)
    expanded: List[str] = []
    for term in terms:
        if _is_noise_term(term):
            continue
        expanded.append(term)
        for key, synonyms in CJK_SYNONYM_MAP.items():
            if key in term:
                expanded.extend(synonyms)
    return _dedupe_keep_order([item for item in expanded if not _is_noise_term(item)])[:24]


def _candidate_terms(text: str) -> List[str]:
    raw = sanitize_text(text or "")
    terms = _extract_ascii_terms(raw) + _extract_cjk_terms(raw)
    return _dedupe_keep_order(terms)[:32]


def _collect_match_terms(query: str, candidate: str, max_items: int = 5) -> List[str]:
    query_terms = _query_terms(query)
    if not query_terms:
        return []
    candidate_terms = set(item.lower() for item in _candidate_terms(candidate))
    candidate_text = sanitize_text(candidate or "").lower()
    hits: List[str] = []
    for term in query_terms:
        normalized = term.lower()
        if len(normalized) < 2:
            continue
        if normalized in candidate_terms or normalized in candidate_text:
            hits.append(term)
    return _dedupe_keep_order(hits)[:max_items]


def _semantic_group_coverage(query: str, candidate: str) -> Tuple[int, int, List[str]]:
    query_text = sanitize_text(query or "").lower()
    candidate_text = sanitize_text(candidate or "").lower()
    query_terms = set(item.lower() for item in _query_terms(query))

    active_groups: List[str] = []
    hit_groups: List[str] = []
    for group, terms in SEMANTIC_GROUPS.items():
        lowered_terms = [item.lower() for item in terms]
        is_active = any(item in query_text for item in lowered_terms) or any(
            item in query_terms for item in lowered_terms
        )
        if not is_active:
            continue
        active_groups.append(group)
        if any(item in candidate_text for item in lowered_terms):
            hit_groups.append(group)

    return len(hit_groups), len(active_groups), hit_groups


def _query_must_groups(query: str) -> List[str]:
    query_text = sanitize_text(query or "").lower()
    must: List[str] = []
    for group, terms in SEMANTIC_GROUPS.items():
        if any(item.lower() in query_text for item in terms):
            must.append(group)
    return _dedupe_keep_order(must)


def _is_community_query(query: str) -> bool:
    lowered = sanitize_text(query or "").lower()
    return any(alias.lower() in lowered for alias in COMMUNITY_QUERY_ALIASES)


def _precision_score(query: str, item: Dict[str, Any]) -> int:
    query_terms = [term.lower() for term in _query_terms(query) if len(term) >= 3]
    if not query_terms:
        return 0
    topics = item.get("topics") or []
    topics_text = " ".join(str(topic) for topic in topics if str(topic).strip())
    name_text = sanitize_text(f"{item.get('full_name') or ''} {topics_text}").lower()
    desc_text = sanitize_text(str(item.get("description") or "")).lower()

    name_hits = 0
    desc_hits = 0
    for term in query_terms:
        if term in name_text:
            name_hits += 1
        elif term in desc_text:
            desc_hits += 1
    weighted_hits = name_hits * 1.35 + desc_hits * 0.65
    raw = weighted_hits / max(1.0, float(len(query_terms)))
    return max(0, min(100, int(round(min(1.0, raw) * 100))))


def _simple_similarity(query: str, candidate: str) -> int:
    query_tokens = _query_terms(query)
    if not query_tokens:
        return 0
    candidate_tokens = set(item.lower() for item in _candidate_terms(candidate))
    candidate_text = sanitize_text(candidate or "").lower()
    overlap = sum(
        1 for token in query_tokens if token.lower() in candidate_tokens or token.lower() in candidate_text
    )
    lexical_coverage = overlap / max(1, len(query_tokens))
    group_hit_count, group_total, hit_groups = _semantic_group_coverage(query, candidate)
    must_groups = _query_must_groups(query)
    missing_must = [group for group in must_groups if group not in hit_groups]
    semantic_coverage = (group_hit_count / group_total) if group_total else 0.0
    phrase = sanitize_text(query or "").strip().lower()
    phrase_bonus = 0.0
    if phrase and len(phrase) >= 4 and phrase in candidate_text:
        phrase_bonus = 0.18
    blended = lexical_coverage * 0.62 + semantic_coverage * 0.38 + phrase_bonus
    if group_total >= 3 and group_hit_count <= 1:
        blended = min(blended, 0.34)
    elif group_total >= 2 and group_hit_count <= 1:
        blended = min(blended, 0.46)
    if len(missing_must) >= 3:
        blended = min(blended, 0.22)
    elif len(missing_must) >= 2:
        blended = min(blended, 0.35)
    elif len(missing_must) == 1:
        blended = min(blended, 0.62)
    score = int(round(min(1.0, blended) * 100))
    return max(0, min(100, score))


def _fallback_rank(candidates: List[Dict[str, Any]], query: str, top_k: int) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for item in candidates:
        summary = item.get("summary") or ""
        similarity = _simple_similarity(query, summary)
        hit_terms = _collect_match_terms(query, summary)
        group_hit_count, group_total, hit_groups = _semantic_group_coverage(query, summary)
        must_groups = _query_must_groups(query)
        missing_must = [group for group in must_groups if group not in hit_groups]
        precision = _precision_score(query, item)
        stars = int(item.get("stars") or 0)
        star_score = min(100, int(math.log10(stars + 1) * 30))
        group_score = int(round((group_hit_count / max(1, group_total)) * 100))
        source_penalty = 8 if str(item.get("source") or "") == "templates" and similarity < 30 else 0
        semantic_penalty = 0
        if group_total >= 3 and group_hit_count <= 1:
            semantic_penalty = 26
        elif group_total >= 2 and group_hit_count <= 1:
            semantic_penalty = 16
        semantic_penalty += len(missing_must) * 9
        if precision < 14 and similarity >= 55:
            semantic_penalty += 12
        # Keyword relevance is the primary ranking signal.
        score = max(
            0,
            int(round(similarity * 0.72 + precision * 0.16 + group_score * 0.10 + star_score * 0.02))
            - source_penalty
            - semantic_penalty,
        )
        reasons: List[str] = []
        if hit_terms:
            reasons.append(f"命中关键词：{', '.join(hit_terms[:4])}")
        if hit_groups:
            labels = [SEMANTIC_GROUP_LABELS.get(name, name) for name in hit_groups]
            reasons.append(f"语义覆盖：{', '.join(labels[:3])}")
        if precision >= 18:
            reasons.append("仓库名/主题词命中较高")
        elif similarity >= 20:
            reasons.append("与需求存在弱关键词重叠")
        else:
            reasons.append("关键词命中较弱，建议人工复核")
        if stars > 100:
            reasons.append("社区热度较高")
        risks: List[str] = []
        if similarity < 18:
            risks.append("与输入关键词的直接重合较弱，请人工复核。")
        if group_total >= 2 and group_hit_count <= 1:
            risks.append("仅覆盖了部分核心语义（微信/采集/情报/汇总），建议人工确认。")
        if missing_must:
            missing_labels = [SEMANTIC_GROUP_LABELS.get(name, name) for name in missing_must[:3]]
            risks.append(f"未覆盖关键语义：{', '.join(missing_labels)}。")
        ranked.append(
            {
                "id": item.get("id"),
                "score": score,
                "reasons": reasons,
                "tags": ["关键词匹配", "社区热度" if stars > 100 else "基础排序"],
                "risks": risks,
            }
        )
    ranked.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return ranked[:top_k]


def _provider_specs() -> List[Tuple[str, Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any]]], str]]:
    specs: List[Tuple[str, Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any]]], str]] = [
        ("github", search_repositories, "GitHub"),
    ]
    if RECOMMEND_ENABLE_GITEE:
        specs.append(("gitee", search_gitee_repositories, "Gitee"))
    if RECOMMEND_ENABLE_GITCODE:
        specs.append(("gitcode", search_gitcode_repositories, "GitCode"))
    return specs


def _normalize_repo_item(item: Dict[str, Any], source: str) -> Dict[str, Any]:
    full_name = str(
        item.get("full_name")
        or item.get("path_with_namespace")
        or item.get("name_with_owner")
        or item.get("id")
        or ""
    ).strip()
    html_url = str(item.get("html_url") or item.get("web_url") or item.get("url") or "").strip()
    if not html_url and full_name:
        if source == "gitee":
            html_url = f"https://gitee.com/{full_name}"
        elif source == "gitcode":
            html_url = f"https://gitcode.com/{full_name}"
        else:
            html_url = f"https://github.com/{full_name}"
    description = str(item.get("description") or "").strip()
    language = str(item.get("language") or "").strip() or None
    topics = _normalize_topics(item.get("topics") or item.get("tag_list"))
    stars = _to_int(item.get("stargazers_count") or item.get("stars_count") or item.get("star_count") or item.get("stars"))
    forks = _to_int(item.get("forks_count") or item.get("forks") or item.get("forks_count"))
    open_issues = _to_int(item.get("open_issues_count") or item.get("open_issues"))
    license_name = _normalize_license(item.get("license"))
    archived = item.get("archived")
    pushed_at = item.get("pushed_at") or item.get("updated_at") or item.get("last_activity_at")
    updated_days = _days_since(pushed_at)
    summary = " ".join(
        [
            full_name,
            description,
            " ".join(topics),
            language or "",
        ]
    ).strip()
    normalized_id = f"{source}:{full_name or html_url or str(item.get('id') or '')}".strip(":")

    return {
        "id": normalized_id,
        "full_name": full_name,
        "html_url": html_url,
        "repo_url": html_url,
        "description": description,
        "language": language,
        "topics": topics,
        "stars": stars,
        "forks": forks,
        "open_issues": open_issues,
        "license": license_name,
        "archived": archived,
        "pushed_at": pushed_at,
        "updated_days": updated_days,
        "summary": summary,
        "source": source,
    }


def _emit_trace(
    trace_steps: List[str],
    progress_callback: Optional[Callable[[str], None]],
    message: str,
) -> None:
    text = str(message or "").strip()
    if not text:
        return
    trace_steps.append(text)
    if progress_callback:
        try:
            progress_callback(text)
        except Exception:
            return


def _search_provider_parallel(
    query_item: str,
    per_page: int,
    timeout: int,
    provider_specs: List[Tuple[str, Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any]]], str]],
) -> List[Tuple[str, str, List[Dict[str, Any]], Optional[Exception]]]:
    if not provider_specs:
        return []
    max_workers = max(1, min(4, len(provider_specs)))
    futures: Dict[Any, Tuple[str, str]] = {}
    results: List[Tuple[str, str, List[Dict[str, Any]], Optional[Exception]]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for source_name, provider_fn, provider_label in provider_specs:
            future = pool.submit(
                provider_fn,
                query_item,
                per_page,
                1,
                timeout,
            )
            futures[future] = (source_name, provider_label)
        for future in as_completed(futures):
            source_name, provider_label = futures[future]
            try:
                items, _ = future.result()
            except Exception as exc:  # noqa: BLE001
                results.append((source_name, provider_label, [], exc))
                continue
            safe_items = [dict(item) for item in (items or []) if isinstance(item, dict)]
            results.append((source_name, provider_label, safe_items, None))
    return results


def _citation_snippet(item: Dict[str, Any], query_terms: List[str]) -> str:
    doc_excerpt = str(item.get("doc_excerpt") or item.get("doc_markdown") or "").strip()
    if doc_excerpt:
        return doc_excerpt[:220]
    description = str(item.get("description") or "").strip()
    if description:
        return description[:180]
    topics = [str(topic) for topic in (item.get("topics") or []) if str(topic).strip()]
    if topics:
        return f"topics: {', '.join(topics[:6])}"
    if query_terms:
        return f"keywords: {', '.join(query_terms[:5])}"
    return str(item.get("full_name") or item.get("id") or "")[:180]


def _build_citations(
    ranked_candidates: List[Dict[str, Any]],
    query_for_score: str,
    max_items: int = 12,
) -> List[RecommendationCitation]:
    citations: List[RecommendationCitation] = []
    query_terms = _query_terms(query_for_score)[:8]
    for item in ranked_candidates[:max_items]:
        repo_id = str(item.get("id") or "").strip()
        title = str(item.get("full_name") or "").strip() or repo_id
        url = str(item.get("html_url") or "").strip()
        doc_url = str(item.get("doc_url") or "").strip()
        if not repo_id or not url:
            continue
        citations.append(
            RecommendationCitation(
                id=repo_id,
                source=str(item.get("source") or "unknown"),
                title=title,
                url=doc_url or url,
                snippet=_citation_snippet(item, query_terms),
                score=int(item.get("match_score") or 0),
                reason=(
                    str((item.get("match_reasons") or [""])[0]).strip()
                    if isinstance(item.get("match_reasons"), list)
                    else None
                ),
            )
        )
    return citations


def _build_deep_insights(
    query_for_score: str,
    requirement_context: str,
    ranked_candidates: List[Dict[str, Any]],
) -> Tuple[Optional[str], List[str], Optional[str]]:
    if not ranked_candidates:
        return None, [], None
    top = ranked_candidates[: min(10, len(ranked_candidates))]
    requirement_anchor = sanitize_text(requirement_context or query_for_score or "").strip()
    if len(requirement_anchor) > 600:
        requirement_anchor = requirement_anchor[:600]
    llm_payload = [
        {
            "id": item.get("id"),
            "name": item.get("full_name"),
            "source": item.get("source"),
            "description": item.get("description"),
            "topics": item.get("topics") or [],
            "language": item.get("language"),
            "match_score": int(item.get("match_score") or 0),
            "doc_excerpt": str(item.get("doc_excerpt") or "")[:600],
        }
        for item in top
    ]
    if llm_available():
        try:
            deep_result = summarize_findings(requirement_anchor or query_for_score[:280], llm_payload, max_points=5)
            if isinstance(deep_result, dict):
                summary = str(deep_result.get("deep_summary") or "").strip() or None
                insight_points = [
                    str(item).strip()
                    for item in (deep_result.get("insight_points") or [])
                    if str(item).strip()
                ][:6]
                if summary or insight_points:
                    return summary, insight_points, None
        except Exception as exc:  # noqa: BLE001
            return None, [], f"深度聚合总结失败，已回退规则总结：{exc}"

    source_mix = _dedupe_keep_order([str(item.get("source") or "unknown") for item in top])
    summary = (
        f"共召回 {len(ranked_candidates)} 个候选，关键词优先排序后保留 {len(top)} 个高相关方案。"
        f"核心需求聚焦：{(requirement_anchor or query_for_score)[:64]}。"
        f"覆盖来源：{', '.join(source_mix[:4])}。"
    )
    points: List[str] = []
    for item in top[:3]:
        title = str(item.get("full_name") or item.get("id") or "").strip()
        score = int(item.get("match_score") or 0)
        reason = ""
        if isinstance(item.get("match_reasons"), list) and item.get("match_reasons"):
            reason = str(item.get("match_reasons")[0] or "").strip()
        alignment_hits = _collect_match_terms(requirement_anchor or query_for_score, str(item.get("summary") or ""))
        alignment_note = f"契合点：{', '.join(alignment_hits[:4])}" if alignment_hits else "契合点：待人工复核"
        parts = [title]
        if reason:
            parts.append(reason)
        parts.append(alignment_note)
        parts.append(f"score={score}")
        points.append(" | ".join(parts))
    return summary, points, None


def _build_profile(requirement_text: str, query: str) -> Tuple[Optional[RecommendationProfile], str, str]:
    profile = build_requirement_profile(requirement_text, query)
    if profile:
        summary = str(profile.get("summary") or "").strip()
        search_query = str(profile.get("search_query") or "").strip()
        return (
            RecommendationProfile(
                summary=summary or None,
                search_query=search_query or None,
                keywords=[str(k) for k in profile.get("keywords") or [] if str(k).strip()],
                must_have=[str(k) for k in profile.get("must_have") or [] if str(k).strip()],
                nice_to_have=[str(k) for k in profile.get("nice_to_have") or [] if str(k).strip()],
                target_stack=[str(k) for k in profile.get("target_stack") or [] if str(k).strip()],
                scenarios=[str(k) for k in profile.get("scenarios") or [] if str(k).strip()],
            ),
            search_query,
            summary,
        )
    query_text = query.strip() or requirement_text[:120].strip()
    return None, query_text, query_text


def _build_search_queries(
    base_query: str,
    normalized_query: str,
    profile: Optional[RecommendationProfile],
) -> List[str]:
    seed_terms: List[str] = []
    if base_query:
        seed_terms.extend(_query_terms(base_query)[:8])
    if normalized_query:
        seed_terms.extend(_query_terms(normalized_query)[:8])
    if profile:
        seed_terms.extend([str(item) for item in (profile.keywords or [])[:4] if str(item).strip()])
        seed_terms.extend([str(item) for item in (profile.must_have or [])[:3] if str(item).strip()])
    if not seed_terms and base_query:
        seed_terms.append(base_query.strip()[:80])
    compact = " ".join(_dedupe_keep_order(seed_terms)).strip()
    compact_query = ""
    if compact:
        compact_query = f"{compact} in:name,description,readme"

    base_for_expand = normalized_query or base_query
    expanded_terms = _query_terms(base_for_expand)
    english_terms = [item for item in expanded_terms if re.search(r"[a-z]", item, re.IGNORECASE)]
    cjk_terms = [item for item in expanded_terms if _contains_cjk(item)]
    expanded_query = ""
    prioritized_english: List[str] = []
    for key, synonyms in CJK_SYNONYM_MAP.items():
        if key in base_for_expand:
            prioritized_english.extend(synonyms[:1])
    if english_terms:
        selected_english = _dedupe_keep_order(prioritized_english + english_terms)[:7]
        selected_cjk = _dedupe_keep_order(cjk_terms)[:2]
        expansion = " ".join(selected_english + selected_cjk).strip()
        if expansion:
            expanded_query = f"{expansion} in:name,description,readme"

    queries: List[str] = []
    if _contains_cjk(base_for_expand) and expanded_query:
        queries.append(expanded_query)
        if compact_query and compact_query.lower() != expanded_query.lower():
            queries.append(compact_query)
    else:
        if compact_query:
            queries.append(compact_query)
        if expanded_query and expanded_query.lower() not in {item.lower() for item in queries}:
            queries.append(expanded_query)

    return _dedupe_keep_order(queries)[:2]


def _normalize_rewritten_queries(raw_queries: List[str]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for item in raw_queries:
        value = sanitize_text(str(item or "")).strip()
        if not value:
            continue
        value = re.sub(r"\s+", " ", value).strip()
        if len(value) < 2:
            continue
        trimmed = value[:96]
        key = trimmed.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(trimmed)
    return normalized[:5]


def _should_rewrite_queries(mode: str, normalized_query: str, requirement_text: str) -> bool:
    if is_deep_search_mode(mode):
        return True
    combined_len = len((normalized_query or "").strip()) + len((requirement_text or "").strip())
    return combined_len > 100


def _is_long_requirement_input(normalized_query: str, requirement_text: str) -> bool:
    combined_len = len((normalized_query or "").strip()) + len((requirement_text or "").strip())
    return combined_len > 100


def _resolve_search_queries(
    *,
    mode: str,
    normalized_query: str,
    requirement_text: str,
    profile: Optional[RecommendationProfile],
    fallback_search_query: str,
    warnings: List[str],
    trace_steps: List[str],
    progress_callback: Optional[Callable[[str], None]],
) -> List[str]:
    should_rewrite = _should_rewrite_queries(mode, normalized_query, requirement_text)
    long_requirement = _is_long_requirement_input(normalized_query, requirement_text)
    rewrite_text_parts = [str(normalized_query or "").strip(), str(requirement_text or "").strip()]
    rewrite_source_text = "\n".join([part for part in rewrite_text_parts if part]).strip()
    if should_rewrite and rewrite_source_text:
        _emit_trace(trace_steps, progress_callback, "启动需求拆解：提取可用于开源检索的技术实现词...")
        try:
            rewritten = extract_search_queries(rewrite_source_text)
            rewritten_queries = _normalize_rewritten_queries([str(item) for item in rewritten])
            if rewritten_queries:
                preview = " | ".join(rewritten_queries[:3])
                _emit_trace(
                    trace_steps,
                    progress_callback,
                    f"需求拆解完成：生成 {len(rewritten_queries)} 条技术检索词（{preview}）。",
                )
                return rewritten_queries
            warnings.append("深度搜索技术词提炼为空：当前输入无法生成可用检索词。")
            if long_requirement:
                _emit_trace(trace_steps, progress_callback, "需求拆解结果为空：已停止低精度降级检索。")
                return []
            _emit_trace(trace_steps, progress_callback, "需求拆解结果为空：已回退关键词检索。")
        except Exception as exc:  # noqa: BLE001
            warnings.append(str(exc))
            if long_requirement:
                _emit_trace(trace_steps, progress_callback, "需求拆解失败：已停止低精度降级检索。")
                return []
            _emit_trace(trace_steps, progress_callback, "需求拆解失败：已回退关键词检索。")

    search_queries = _build_search_queries(fallback_search_query, normalized_query, profile)
    if not search_queries and fallback_search_query:
        fallback = sanitize_text(str(fallback_search_query or "")).strip()[:96]
        if fallback:
            search_queries = [fallback]
    return _normalize_rewritten_queries(search_queries)


def _search_multi_query_provider_parallel(
    *,
    search_queries: List[str],
    per_page_base: int,
    timeout: int,
    provider_specs: List[Tuple[str, Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any]]], str]],
) -> List[Tuple[int, str, str, str, List[Dict[str, Any]], Optional[Exception]]]:
    if not search_queries or not provider_specs:
        return []
    futures: Dict[Any, Tuple[int, str, str, str]] = {}
    results: List[Tuple[int, str, str, str, List[Dict[str, Any]], Optional[Exception]]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(12, len(search_queries) * len(provider_specs)))) as pool:
        for idx, query_item in enumerate(search_queries):
            query_per_page = max(6, int(per_page_base / (idx + 1)))
            for source_name, provider_fn, provider_label in provider_specs:
                future = pool.submit(
                    provider_fn,
                    query_item,
                    query_per_page,
                    1,
                    timeout,
                )
                futures[future] = (idx, query_item, source_name, provider_label)
        for future in as_completed(futures):
            idx, query_item, source_name, provider_label = futures[future]
            try:
                items, _ = future.result()
            except Exception as exc:  # noqa: BLE001
                results.append((idx, query_item, source_name, provider_label, [], exc))
                continue
            safe_items = [dict(item) for item in (items or []) if isinstance(item, dict)]
            results.append((idx, query_item, source_name, provider_label, safe_items, None))
    return results


def recommend_repositories(
    query: str,
    requirement_text: str,
    mode: str,
    limit: int,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> RecommendationResponse:
    normalized_query = (query or "").strip()
    requirement_text = sanitize_text(requirement_text or "")
    requirement_text = requirement_text[:RECOMMEND_MAX_TEXT_CHARS]
    warnings: List[str] = []
    trace_steps: List[str] = []
    deep_summary: Optional[str] = None
    insight_points: List[str] = []
    citations: List[RecommendationCitation] = []

    _emit_trace(trace_steps, progress_callback, "解析输入并提取关键词画像...")
    try:
        profile, search_query, summary = _build_profile(requirement_text, normalized_query)
    except Exception as exc:
        profile = None
        fallback_text = normalized_query or requirement_text[:120].strip()
        search_query = fallback_text
        summary = fallback_text
        warnings.append("语义画像生成失败，已降级为关键词匹配。")
        warnings.append(str(exc))
        _emit_trace(trace_steps, progress_callback, "语义画像降级：已切换关键词检索。")
    search_query = search_query.strip() or normalized_query or requirement_text[:120].strip()
    rewrite_required = _should_rewrite_queries(mode, normalized_query, requirement_text)
    long_requirement = _is_long_requirement_input(normalized_query, requirement_text)
    search_queries = _resolve_search_queries(
        mode=mode,
        normalized_query=normalized_query,
        requirement_text=requirement_text,
        profile=profile,
        fallback_search_query=search_query,
        warnings=warnings,
        trace_steps=trace_steps,
        progress_callback=progress_callback,
    )
    _emit_trace(
        trace_steps,
        progress_callback,
        f"已生成 {len(search_queries)} 条检索表达式，启动并发多源搜索。",
    )
    if rewrite_required and long_requirement and not search_queries:
        warnings.append("深度搜索需要可用的技术词提炼结果；请配置 MINIMAX_API_KEY 或 OPENAI_API_KEY 后重试。")
        return RecommendationResponse(
            request_id=f"rec-{int(_now_ts())}",
            query=normalized_query or None,
            mode=mode,
            generated_at=_now_ts(),
            requirement_excerpt=requirement_text[:200] or None,
            search_query=None,
            profile=profile,
            warnings=_dedupe_keep_order(warnings),
            sources=[],
            deep_summary=None,
            insight_points=[],
            trace_steps=trace_steps,
            citations=[],
            recommendations=[],
        )

    sources: List[str] = []
    candidates: List[Dict[str, Any]] = []
    seen_candidate_ids: set[str] = set()
    warned_providers: set[str] = set()
    provider_specs = _provider_specs()
    for idx, query_item in enumerate(search_queries):
        _emit_trace(trace_steps, progress_callback, f"检索式 {idx + 1}/{len(search_queries)}：{query_item[:72]}")

    search_results = _search_multi_query_provider_parallel(
        search_queries=search_queries,
        per_page_base=RECOMMEND_GITHUB_MAX_RESULTS,
        timeout=RECOMMEND_PROVIDER_TIMEOUT_SECONDS,
        provider_specs=provider_specs,
    )
    for _idx, _query_item, source_name, provider_label, items, error in search_results:
        if error is not None:
            if source_name in warned_providers:
                continue
            warned_providers.add(source_name)
            warnings.append(f"{provider_label} 搜索失败，推荐结果可能不完整。")
            warnings.append(str(getattr(error, "message", error)))
            continue
        for item in items:
            normalized_item = _normalize_repo_item(item, source=source_name)
            repo_id = str(normalized_item.get("id") or "")
            if not repo_id or repo_id in seen_candidate_ids:
                continue
            seen_candidate_ids.add(repo_id)
            candidates.append(normalized_item)
        if items and source_name not in sources:
            sources.append(source_name)
            _emit_trace(trace_steps, progress_callback, f"{provider_label} 命中 {len(items)} 条候选。")

    if len(candidates) < max(6, limit * 2):
        templates = load_templates()
        for item in templates:
            repo_url = str(item.get("repo_url") or "")
            full_name = _parse_full_name(repo_url)
            if not full_name:
                continue
            normalized_item = _normalize_repo_item(
                {
                    "full_name": full_name,
                    "html_url": repo_url,
                    "description": item.get("description"),
                    "topics": item.get("tags") or item.get("dimensions") or [],
                },
                source="templates",
            )
            normalized_id = str(normalized_item.get("id") or "")
            if not normalized_id or normalized_id in seen_candidate_ids:
                continue
            seen_candidate_ids.add(normalized_id)
            candidates.append(normalized_item)
        if templates:
            sources.append("templates")
            _emit_trace(trace_steps, progress_callback, "目录模板补充召回已启用。")

    _emit_trace(
        trace_steps,
        progress_callback,
        f"多源召回完成，候选池总数 {len(candidates)}。",
    )

    if not candidates:
        return RecommendationResponse(
            request_id=f"rec-{int(_now_ts())}",
            query=normalized_query or None,
            mode=mode,
            generated_at=_now_ts(),
            requirement_excerpt=requirement_text[:200] or None,
            search_query=search_query or None,
            profile=profile,
            warnings=warnings or ["未检索到可用仓库，请调整需求描述。"],
            sources=sources,
            deep_summary=deep_summary,
            insight_points=insight_points,
            trace_steps=trace_steps,
            citations=citations,
            recommendations=[],
        )

    candidate_summaries = [
        {
            "id": item["id"],
            "name": item["full_name"],
            "description": item["description"],
            "topics": item["topics"],
            "language": item["language"],
            "stars": item["stars"],
        }
        for item in candidates
    ]

    # Product requirement: keep result set >= 10 (if candidate pool allows).
    top_k = max(10, min(int(limit or RECOMMEND_TOP_K), 20))
    ranking = None
    _emit_trace(trace_steps, progress_callback, "开始关键词优先排序与语义重排...")
    if llm_available():
        try:
            ranking = rank_candidates(summary or normalized_query or requirement_text[:120], candidate_summaries, top_k)
        except Exception:
            warnings.append("语义匹配失败，已降级为关键词排序。")
    else:
        warnings.append("未配置语义模型（OPENAI_API_KEY 缺失或不可用），已使用关键词匹配。")

    if ranking and isinstance(ranking.get("results"), list):
        ranked_items = ranking["results"]
    else:
        ranked_items = _fallback_rank(candidates, summary or normalized_query or requirement_text, top_k)

    if search_queries:
        query_for_score = " ".join(search_queries[:5])
    else:
        query_for_score = summary or normalized_query or requirement_text
    sorted_candidates: List[Dict[str, Any]] = []
    for item in ranked_items:
        repo_id = item.get("id")
        if not repo_id:
            continue
        match = next((c for c in candidates if c["id"] == repo_id), None)
        if not match:
            continue
        summary_text = str(match.get("summary") or "")
        lexical_score = _simple_similarity(query_for_score, summary_text)
        precision_score = _precision_score(query_for_score, match)
        group_hit_count, group_total, hit_groups = _semantic_group_coverage(query_for_score, summary_text)
        must_groups = _query_must_groups(query_for_score)
        missing_must = [group for group in must_groups if group not in hit_groups]
        must_coverage = int(round((len(must_groups) - len(missing_must)) / max(1, len(must_groups)) * 100))
        model_score = int(item.get("score") or 0)
        if ranking and isinstance(ranking.get("results"), list):
            final_score = int(
                round(
                    lexical_score * 0.52
                    + precision_score * 0.18
                    + must_coverage * 0.10
                    + model_score * 0.20
                )
            )
        else:
            final_score = int(
                round(
                    lexical_score * 0.56
                    + precision_score * 0.12
                    + must_coverage * 0.08
                    + model_score * 0.24
                )
            )
        if str(match.get("source") or "") == "templates" and lexical_score < 30:
            final_score = max(0, final_score - 8)
        if group_total >= 2 and group_hit_count <= 1:
            final_score = max(0, final_score - 12)
        if missing_must:
            final_score = max(0, final_score - len(missing_must) * 8)

        hit_terms = _collect_match_terms(query_for_score, summary_text)
        reasons = [str(r) for r in (item.get("reasons") or []) if str(r).strip()]
        if hit_terms:
            reasons.insert(0, f"命中关键词：{', '.join(hit_terms[:4])}")
        elif not reasons:
            reasons.append("关键词命中较弱，主要依赖语义相似。")

        risk_notes = [str(r) for r in (item.get("risks") or []) if str(r).strip()]
        if lexical_score < 18:
            risk_notes.append("与输入关键词的直接重合较弱，请人工复核。")
        if missing_must:
            missing_labels = [SEMANTIC_GROUP_LABELS.get(name, name) for name in missing_must[:3]]
            risk_notes.append(f"未覆盖关键语义：{', '.join(missing_labels)}。")
        if precision_score < 12 and lexical_score >= 45:
            risk_notes.append("仓库名与主题词命中偏弱，可能存在语义漂移。")

        match["match_score"] = max(0, min(100, final_score))
        match["keyword_score"] = lexical_score
        match["match_reasons"] = _dedupe_keep_order(reasons)
        match["match_tags"] = _dedupe_keep_order([str(r) for r in (item.get("tags") or []) if str(r).strip()])
        match["risk_notes"] = _dedupe_keep_order(risk_notes)
        sorted_candidates.append(match)

    pre_guardrail_candidates = list(sorted_candidates)
    if not sorted_candidates:
        sorted_candidates = candidates[:top_k]
        for item in sorted_candidates:
            item["match_score"] = 0
            item["keyword_score"] = 0
            item["match_reasons"] = []
            item["match_tags"] = []
            item["risk_notes"] = []
    else:
        sorted_candidates.sort(
            key=lambda row: (
                int(row.get("keyword_score") or 0),
                int(row.get("match_score") or 0),
                int(row.get("stars") or 0),
            ),
            reverse=True,
        )
        sorted_candidates = sorted_candidates[:top_k]

    must_groups = _query_must_groups(query_for_score)
    if must_groups and sorted_candidates:
        min_group_hits = max(2, math.ceil(len(must_groups) * 0.5))
        hard_groups = [group for group in must_groups if group in {"wechat", "crawl"}]
        filtered_candidates: List[Dict[str, Any]] = []
        for item in sorted_candidates:
            summary_text = str(item.get("summary") or "")
            _, _, hit_groups = _semantic_group_coverage(query_for_score, summary_text)
            if any(group not in hit_groups for group in hard_groups):
                continue
            if len(hit_groups) >= min_group_hits and int(item.get("match_score") or 0) >= 12:
                filtered_candidates.append(item)
        if filtered_candidates:
            if len(filtered_candidates) < len(sorted_candidates):
                warnings.append("已过滤部分语义偏离仓库，结果更聚焦于核心关键词。")
            sorted_candidates = filtered_candidates
            if len(sorted_candidates) < top_k:
                seen_ids = {str(item.get("id") or "") for item in sorted_candidates}
                for item in pre_guardrail_candidates:
                    item_id = str(item.get("id") or "")
                    if not item_id or item_id in seen_ids:
                        continue
                    summary_text = str(item.get("summary") or "")
                    _, _, hit_groups = _semantic_group_coverage(query_for_score, summary_text)
                    if any(group not in hit_groups for group in hard_groups):
                        continue
                    if int(item.get("keyword_score") or 0) < 24:
                        continue
                    sorted_candidates.append(item)
                    seen_ids.add(item_id)
                    if len(sorted_candidates) >= top_k:
                        break
                if len(sorted_candidates) > len(filtered_candidates):
                    warnings.append("结果已按关键词匹配补齐，优先保证搜索覆盖数量。")
        elif hard_groups:
            labels = [SEMANTIC_GROUP_LABELS.get(group, group) for group in hard_groups]
            warnings.append(f"未检索到同时覆盖核心语义（{' + '.join(labels)}）的仓库，请补充更具体约束。")
            sorted_candidates = []
            if _is_community_query(query_for_score):
                relaxed: List[Dict[str, Any]] = []
                for item in pre_guardrail_candidates:
                    text = sanitize_text(str(item.get("summary") or "")).lower()
                    if not any(alias.lower() in text for alias in COMMUNITY_QUERY_ALIASES):
                        continue
                    relaxed.append(item)
                    if len(relaxed) >= top_k:
                        break
                if relaxed:
                    warnings.append("严格护栏无结果，已回退到社区关键词候选补齐。")
                    sorted_candidates = relaxed

    citations = _build_citations(sorted_candidates, query_for_score, max_items=12)
    _emit_trace(
        trace_steps,
        progress_callback,
        f"排序完成，输出 {len(sorted_candidates)} 条结果，生成 {len(citations)} 条引文。",
    )

    if is_deep_search_mode(mode):
        _emit_trace(trace_steps, progress_callback, "深度模式：补全仓库详情并执行聚合提炼...")
        for item in sorted_candidates:
            detail: Dict[str, Any] = {}
            try:
                detail = fetch_repo(item["full_name"])
            except Exception:
                detail = {}
            if detail:
                item.update(_normalize_repo_item(detail, source=item.get("source") or "github"))
        _emit_trace(trace_steps, progress_callback, "深度模式：抓取外部仓库 README/网页文档片段...")
        doc_warnings = enrich_candidates_with_documents(
            sorted_candidates,
            top_n=RECOMMEND_DEEP_DOC_FETCH_LIMIT,
            timeout=RECOMMEND_DEEP_DOC_TIMEOUT_SECONDS,
            progress_callback=progress_callback,
        )
        for warning in doc_warnings[:12]:
            warnings.append(f"文档抓取提示：{warning}")
        citations = _build_citations(sorted_candidates, query_for_score, max_items=12)
        deep_summary, insight_points, deep_warning = _build_deep_insights(
            query_for_score,
            requirement_text or normalized_query or query_for_score,
            sorted_candidates,
        )
        if deep_warning:
            warnings.append(deep_warning)
        if not deep_summary:
            deep_summary = "深度分析已完成，结果按关键词匹配优先并结合语义信号排序。"

    recommendations: List[RepoRecommendation] = []
    for item in sorted_candidates:
        health = _compute_health_card(item)
        recommendations.append(
            RepoRecommendation(
                id=item["id"],
                full_name=item["full_name"],
                html_url=item["html_url"],
                repo_url=str(item.get("repo_url") or item.get("html_url") or "").strip() or None,
                description=item.get("description") or None,
                language=item.get("language"),
                topics=item.get("topics") or [],
                stars=int(item.get("stars") or 0),
                forks=int(item.get("forks") or 0),
                open_issues=int(item.get("open_issues") or 0),
                license=item.get("license"),
                archived=item.get("archived"),
                pushed_at=item.get("pushed_at"),
                updated_days=item.get("updated_days"),
                match_score=int(item.get("match_score") or 0),
                match_reasons=item.get("match_reasons") or [],
                match_tags=item.get("match_tags") or [],
                risk_notes=item.get("risk_notes") or [],
                health=health,
                source=item.get("source"),
            )
        )

    return RecommendationResponse(
        request_id=f"rec-{int(_now_ts())}",
        query=normalized_query or None,
        mode=mode,
        generated_at=_now_ts(),
        requirement_excerpt=requirement_text[:200] or None,
        search_query=(search_queries[0] if search_queries else search_query) or None,
        profile=profile,
        warnings=warnings,
        sources=sources,
        deep_summary=deep_summary,
        insight_points=insight_points,
        trace_steps=trace_steps,
        citations=citations,
        recommendations=recommendations,
    )
