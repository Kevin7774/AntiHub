from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from config import RECOMMEND_EXTERNAL_SOURCES_ENABLED
from recommend.llm import build_requirement_profile, llm_available, rank_candidates
from recommend.models import (
    CapabilityTag,
    RecommendationAction,
    RecommendationCitation,
    RecommendationProfile,
    RecommendationResponse,
    RepoHealthCard,
    RepoRecommendation,
    RepoScoreMetric,
    ScoreBreakdown,
)
from recommend.service import recommend_repositories

from .db import session_scope
from .models import Capability, Case, ProductActionType, ProductType
from .repository import DecisionRepository

SCORE_WEIGHTS = {
    "relevance": 0.4,
    "popularity": 0.2,
    "cost_bonus": 0.15,
    "capability_match": 0.25,
}

DEFAULT_CAPABILITY_ALIASES: dict[str, list[str]] = {
    "payment_gateway": ["支付", "扫码支付", "payment", "gateway", "wechat pay", "alipay"],
    "points_management": ["积分", "积分管理", "points", "loyalty"],
    "coupon_management": ["卡券", "优惠券", "coupon", "voucher"],
    "audit_log": ["审计", "审计日志", "audit", "traceability", "compliance"],
    "merchant_portal": ["商户端", "门店端", "merchant", "store"],
    "resident_portal": ["居民端", "业主", "resident", "consumer"],
    "government_subsidy": ["补贴", "政府", "subsidy", "government", "fund pool"],
    "split_settlement": ["分账", "清分", "settlement", "revenue sharing"],
    "inventory_sku": ["sku", "库存", "商品", "inventory", "catalog"],
    "cms_builder": ["cms", "页面装修", "可视化", "content", "page builder"],
    "rbac": ["权限", "角色", "rbac", "access control"],
    "community_portal": ["社区", "社群", "论坛", "community", "forum", "bbs", "数字化社区"],
    "community_moderation": ["内容审核", "内容治理", "moderation", "ugc", "帖子", "评论", "话题"],
}

FAST_MODE_NOTICE = "AI 服务繁忙，已切换至极速模式（关键词检索）。"

TERM_STOPWORDS = {
    "项目",
    "系统",
    "平台",
    "方案",
    "支持",
    "功能",
    "实现",
    "需求",
    "需要",
    "我们",
    "你们",
    "他们",
    "this",
    "that",
    "with",
    "for",
    "from",
    "project",
    "system",
    "platform",
}

INTENT_GROUP_ALIASES: dict[str, list[str]] = {
    "community": ["社区", "社群", "论坛", "digital community", "community", "forum", "bbs", "圈子"],
    "erp": ["erp", "进销存", "库存", "财务", "采购", "供应链", "仓储"],
    "crm": ["crm", "客户关系", "线索", "客户管理", "salesforce", "hubspot"],
    "ecommerce": ["电商", "商城", "订单", "商品", "shop", "commerce"],
}

INTENT_GROUP_LABELS: dict[str, str] = {
    "community": "社区",
    "erp": "ERP",
    "crm": "CRM",
    "ecommerce": "电商",
}


@dataclass(frozen=True)
class _CaseScore:
    case: Case
    relevance: int
    popularity: int
    cost_bonus: int
    capability_match: int
    final_score: int
    matched_capabilities: list[str]
    matched_intents: list[str]


@dataclass(frozen=True)
class _ExternalRecall:
    recommendations: list[RepoRecommendation]
    sources: list[str]
    warnings: list[str]
    deep_summary: Optional[str]
    insight_points: list[str]
    citations: list[RecommendationCitation]
    trace_steps: list[str]


def _now_ts() -> float:
    return time.time()


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        token = str(item or "").strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _extract_ascii_terms(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9][a-z0-9_\-]{1,31}", text)
    return [token for token in raw if token not in TERM_STOPWORDS]


def _extract_cjk_terms(text: str) -> list[str]:
    terms: list[str] = []
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    for chunk in chunks:
        if chunk in TERM_STOPWORDS:
            continue
        terms.append(chunk)
        # Expand long Chinese chunks into short n-grams so phrases like
        # "数字化社区" still hit "社区" in candidate summaries.
        if len(chunk) >= 4:
            for gram_size in (2, 3):
                for index in range(0, len(chunk) - gram_size + 1):
                    gram = chunk[index : index + gram_size]
                    if gram in TERM_STOPWORDS:
                        continue
                    terms.append(gram)
    return terms


def _extract_terms(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    mixed_tokens = re.findall(r"[a-z0-9\u4e00-\u9fff][a-z0-9_\-\u4e00-\u9fff]{1,31}", normalized)
    terms: list[str] = [token for token in mixed_tokens if token not in TERM_STOPWORDS]
    terms.extend(_extract_ascii_terms(normalized))
    terms.extend(_extract_cjk_terms(normalized))

    for aliases in INTENT_GROUP_ALIASES.values():
        lowered = [_normalize_text(alias) for alias in aliases]
        if any(alias and alias in normalized for alias in lowered):
            terms.extend(lowered)
    return _dedupe_keep_order(terms)[:64]


def _grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def _bounded(value: int) -> int:
    return max(0, min(100, int(value)))


def _collect_case_text(case: Case, capability_names: Iterable[str]) -> str:
    payload = " ".join(
        [
            str(case.title or ""),
            str(case.summary or ""),
            str(case.vendor or ""),
            str(case.slug or ""),
            " ".join(capability_names),
            json.dumps(case.metadata_json or {}, ensure_ascii=False),
        ]
    )
    return _normalize_text(payload)


def _infer_requested_capability_codes(requirement_text: str, capabilities: list[Capability]) -> list[str]:
    haystack = _normalize_text(requirement_text)
    if not haystack:
        return []
    terms = set(_extract_terms(requirement_text))
    requested: list[str] = []
    for capability in capabilities:
        aliases: list[str] = []
        aliases.append(str(capability.code or "").lower())
        aliases.append(str(capability.name or "").lower())
        aliases.extend(str(item).strip().lower() for item in (capability.aliases_json or []))
        aliases.extend(DEFAULT_CAPABILITY_ALIASES.get(str(capability.code or "").lower(), []))
        for alias in aliases:
            alias_value = _normalize_text(alias)
            if len(alias_value) < 2:
                continue
            if alias_value in haystack or alias_value in terms:
                requested.append(capability.code)
                break
    return _dedupe_keep_order(requested)


def _infer_query_intents(query_text: str) -> list[str]:
    normalized = _normalize_text(query_text)
    if not normalized:
        return []
    terms = set(_extract_terms(query_text))
    intents: list[str] = []
    for intent, aliases in INTENT_GROUP_ALIASES.items():
        lowered = [_normalize_text(alias) for alias in aliases]
        if any(alias and (alias in normalized or alias in terms) for alias in lowered):
            intents.append(intent)
    return _dedupe_keep_order(intents)


def _infer_hard_intents_from_llm(query_text: str, requirement_text: str, query_intents: list[str]) -> list[str]:
    """
    Resolve strict intent guardrails dynamically from LLM requirement understanding.

    We intentionally avoid hardcoded default intents. Guardrails are only enabled
    when LLM inference confirms concrete domain intent signals.
    """

    if not query_intents or not llm_available():
        return []
    source_requirement = str(requirement_text or "").strip() or str(query_text or "").strip()
    # Guardrails are only enabled when long-form PRD context is available.
    if len(source_requirement) < 100:
        return []
    source_query = str(query_text or "").strip() or source_requirement[:120]
    try:
        profile = build_requirement_profile(source_requirement, source_query)
    except Exception:
        return []
    if not isinstance(profile, dict):
        return []
    profile_text_parts = [
        str(profile.get("summary") or ""),
        str(profile.get("search_query") or ""),
        " ".join(str(item) for item in (profile.get("keywords") or []) if str(item).strip()),
        " ".join(str(item) for item in (profile.get("must_have") or []) if str(item).strip()),
        " ".join(str(item) for item in (profile.get("target_stack") or []) if str(item).strip()),
    ]
    profile_text = " ".join(part for part in profile_text_parts if part).strip()
    if not profile_text:
        return []
    llm_intents = set(_infer_query_intents(profile_text))
    return [intent for intent in query_intents if intent in llm_intents]


def _match_case_intents(case_text: str, query_intents: list[str]) -> list[str]:
    if not query_intents:
        return []
    hits: list[str] = []
    for intent in query_intents:
        aliases = [_normalize_text(alias) for alias in INTENT_GROUP_ALIASES.get(intent, [])]
        if any(alias and alias in case_text for alias in aliases):
            hits.append(intent)
    return _dedupe_keep_order(hits)


def _score_relevance(
    query_text: str,
    case_text: str,
    capability_overlap: int,
    *,
    query_intent_total: int = 0,
    matched_intent_count: int = 0,
) -> int:
    query_terms = _extract_terms(query_text)
    if not query_terms:
        return 35
    overlap = sum(1 for token in query_terms if token in case_text)
    lexical = int(round((overlap / max(1, len(query_terms))) * 100))
    bonus = min(30, capability_overlap * 12)
    intent_bonus = 0
    if query_intent_total > 0:
        intent_ratio = matched_intent_count / max(1, query_intent_total)
        intent_bonus = int(round(intent_ratio * 18))
    score = _bounded(int(round(lexical * 0.72 + bonus + intent_bonus)))
    if query_intent_total and matched_intent_count == 0:
        score = min(score, 22)
    elif query_intent_total >= 2 and matched_intent_count == 1:
        score = min(score, 48)
    return score


def _score_cost_bonus(case: Case) -> int:
    if case.cost_bonus_override is not None:
        return _bounded(case.cost_bonus_override)

    if case.product_type == ProductType.OPEN_SOURCE:
        return 95
    if case.product_type == ProductType.PRIVATE_SOLUTION:
        return 60

    monthly = case.estimated_monthly_cost_cents
    if monthly is None:
        return 68
    if monthly <= 100_00:
        return 78
    if monthly <= 500_00:
        return 68
    if monthly <= 2000_00:
        return 58
    return 45


def _score_capability_match(requested_codes: list[str], case_codes: set[str]) -> tuple[int, list[str]]:
    if not requested_codes:
        return 35, []
    hits = [code for code in requested_codes if code in case_codes]
    ratio = len(hits) / max(1, len(requested_codes))
    return _bounded(int(round(ratio * 100))), hits


def _score_case(
    case: Case,
    query_text: str,
    requested_codes: list[str],
    query_intents: list[str],
) -> _CaseScore:
    case_codes = {
        str(link.capability.code).strip().lower()
        for link in (case.capabilities or [])
        if link.capability and str(link.capability.code or "").strip()
    }
    capability_score, matched_capabilities = _score_capability_match(requested_codes, case_codes)
    capability_names = [
        str(link.capability.name or "")
        for link in (case.capabilities or [])
        if link.capability and str(link.capability.name or "").strip()
    ]
    case_text = _collect_case_text(case, capability_names)
    matched_intents = _match_case_intents(case_text, query_intents)
    relevance = _score_relevance(
        query_text,
        case_text,
        len(matched_capabilities),
        query_intent_total=len(query_intents),
        matched_intent_count=len(matched_intents),
    )
    popularity = _bounded(case.popularity_score)
    cost_bonus = _score_cost_bonus(case)
    final_score = _bounded(
        int(
            round(
                relevance * SCORE_WEIGHTS["relevance"]
                + popularity * SCORE_WEIGHTS["popularity"]
                + cost_bonus * SCORE_WEIGHTS["cost_bonus"]
                + capability_score * SCORE_WEIGHTS["capability_match"]
            )
        )
    )
    if query_intents and not matched_intents:
        final_score = min(final_score, 24)
    elif len(query_intents) >= 2 and len(matched_intents) == 1:
        final_score = min(final_score, 52)
    if not requested_codes and relevance < 18:
        final_score = min(final_score, 30)
    return _CaseScore(
        case=case,
        relevance=relevance,
        popularity=popularity,
        cost_bonus=cost_bonus,
        capability_match=capability_score,
        final_score=final_score,
        matched_capabilities=matched_capabilities,
        matched_intents=matched_intents,
    )


def _apply_intent_guardrails(
    scored: list[_CaseScore],
    query_intents: list[str],
    hard_intents: list[str],
) -> tuple[list[_CaseScore], list[str]]:
    if not scored or not query_intents or not hard_intents:
        return scored, []
    warnings: list[str] = []
    required_hits = 1 if len(query_intents) <= 2 else 2
    filtered: list[_CaseScore] = []
    for row in scored:
        matched = set(row.matched_intents)
        if any(intent not in matched for intent in hard_intents):
            continue
        if len(matched) >= required_hits:
            filtered.append(row)
    labels = [INTENT_GROUP_LABELS.get(intent, intent) for intent in query_intents]
    if filtered:
        if len(filtered) < len(scored):
            warnings.append(f"已启用语义护栏，仅保留与核心意图（{'/'.join(labels)}）一致的方案。")
        return filtered, warnings
    warnings.append(f"未检索到与核心意图（{'/'.join(labels)}）一致的方案，请补充更具体需求。")
    return [], warnings


def _build_health_card(score: _CaseScore) -> RepoHealthCard:
    return RepoHealthCard(
        overall_score=score.final_score,
        grade=_grade(score.final_score),
        activity=RepoScoreMetric(
            label="场景相关性",
            score=score.relevance,
            status="高" if score.relevance >= 80 else "中" if score.relevance >= 60 else "低",
            value=f"{score.relevance}/100",
        ),
        community=RepoScoreMetric(
            label="生态热度",
            score=score.popularity,
            status="高" if score.popularity >= 80 else "中" if score.popularity >= 60 else "低",
            value=f"{score.popularity}/100",
        ),
        maintenance=RepoScoreMetric(
            label="能力匹配",
            score=score.capability_match,
            status="高" if score.capability_match >= 80 else "中" if score.capability_match >= 60 else "低",
            value=f"{score.capability_match}/100",
        ),
        warnings=[],
        signals=[
            f"成本友好度 {score.cost_bonus}/100",
            f"综合评分 {score.final_score}/100",
        ],
    )


def _build_action(case: Case) -> RecommendationAction:
    official = str(case.official_url or "").strip() or None
    repo_url = str(case.repo_url or "").strip() or None

    if case.action_type == ProductActionType.ONE_CLICK_DEPLOY:
        return RecommendationAction(
            action_type=ProductActionType.ONE_CLICK_DEPLOY.value,
            label="一键部署",
            url=repo_url or official,
            deploy_supported=bool(repo_url),
            detail="开源方案支持部署动作，商业方案将优雅降级为查看官网。",
        )
    if case.action_type == ProductActionType.CONTACT_SOLUTION:
        return RecommendationAction(
            action_type=ProductActionType.CONTACT_SOLUTION.value,
            label="咨询方案",
            url=official,
            deploy_supported=False,
            detail="私有化方案通常需要方案咨询与定制交付。",
        )
    return RecommendationAction(
        action_type=ProductActionType.VISIT_OFFICIAL_SITE.value,
        label="查看方案",
        url=official or repo_url,
        deploy_supported=False,
        detail="商业产品默认跳转官网，避免误触发部署动作。",
    )


def _build_reasons(
    score: _CaseScore,
    requested_codes: list[str],
    case_codes: list[str],
    query_intents: list[str],
) -> list[str]:
    reasons = [
        f"相关性 {score.relevance}/100，能力匹配 {score.capability_match}/100。",
        f"热度 {score.popularity}/100，成本友好度 {score.cost_bonus}/100。",
        f"综合评分 = 相关性*0.4 + 热度*0.2 + 成本*0.15 + 能力匹配*0.25 = {score.final_score}",
    ]
    if query_intents:
        if score.matched_intents:
            labels = [INTENT_GROUP_LABELS.get(intent, intent) for intent in score.matched_intents]
            reasons.append(f"命中语义意图：{', '.join(labels)}")
        else:
            reasons.append("语义意图命中较弱，建议人工确认。")
    if requested_codes:
        hit_names = ", ".join(score.matched_capabilities[:5]) or "无"
        reasons.append(f"命中能力标签：{hit_names}")
    elif case_codes:
        reasons.append(f"候选能力覆盖：{', '.join(case_codes[:5])}")
    return reasons


def _build_risks(case: Case, score: _CaseScore, requested_codes: list[str], query_intents: list[str]) -> list[str]:
    risks: list[str] = []
    if requested_codes and score.capability_match < 50:
        risks.append("能力标签覆盖不足，建议补充更具体约束后复检。")
    if query_intents and not score.matched_intents:
        labels = [INTENT_GROUP_LABELS.get(intent, intent) for intent in query_intents]
        risks.append(f"未覆盖关键语义意图（{'/'.join(labels)}），建议谨慎评估。")
    if case.product_type == ProductType.COMMERCIAL and (case.estimated_monthly_cost_cents or 0) > 1000_00:
        risks.append("商业产品预计成本较高，请评估预算与ROI。")
    if case.product_type == ProductType.OPEN_SOURCE and not case.repo_url:
        risks.append("缺少仓库地址，暂时无法执行部署动作。")
    return risks


def _case_capability_codes(case: Case) -> list[str]:
    return [
        str(link.capability.code).strip().lower()
        for link in (case.capabilities or [])
        if link.capability and str(link.capability.code or "").strip()
    ]


def _build_ai_candidate_payload(row: _CaseScore) -> dict[str, Any]:
    case = row.case
    return {
        "id": case.id,
        "name": case.title,
        "summary": str(case.summary or ""),
        "product_type": case.product_type.value,
        "capabilities": _case_capability_codes(case),
        "score": row.final_score,
    }


def _rerank_with_ai(
    *,
    scored: list[_CaseScore],
    query_text: str,
    top_k: int,
) -> tuple[list[_CaseScore], bool, list[str]]:
    if not scored:
        return [], False, []
    if not llm_available():
        return scored[:top_k], False, []

    candidate_pool = scored[: max(top_k * 3, top_k)]
    candidates = [_build_ai_candidate_payload(row) for row in candidate_pool]
    try:
        ranking = rank_candidates(query_text, candidates, top_k)
    except Exception:  # noqa: BLE001
        return scored[:top_k], False, [FAST_MODE_NOTICE]
    if not ranking:
        return scored[:top_k], False, [FAST_MODE_NOTICE]
    results = ranking.get("results")
    if not isinstance(results, list):
        return scored[:top_k], False, [FAST_MODE_NOTICE]

    rows_by_id = {row.case.id: row for row in candidate_pool}
    ordered: list[_CaseScore] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("id") or "").strip()
        if not candidate_id or candidate_id in seen:
            continue
        row = rows_by_id.get(candidate_id)
        if not row:
            continue
        ordered.append(row)
        seen.add(candidate_id)

    if not ordered:
        return scored[:top_k], False, [FAST_MODE_NOTICE]

    for row in scored:
        row_id = row.case.id
        if row_id in seen:
            continue
        ordered.append(row)
        seen.add(row_id)
        if len(ordered) >= top_k:
            break

    return ordered[:top_k], True, []


def _build_external_match_text(item: RepoRecommendation) -> str:
    parts = [
        str(item.full_name or ""),
        str(item.description or ""),
        " ".join(str(topic) for topic in (item.topics or []) if str(topic).strip()),
        str(item.language or ""),
    ]
    return _normalize_text(" ".join(parts))


def _dedupe_keep_order_text(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in items:
        value = str(raw or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(value)
    return deduped


def _emit_progress(progress_callback: Optional[Callable[[str], None]], message: str) -> None:
    text = str(message or "").strip()
    if not text or progress_callback is None:
        return
    try:
        progress_callback(text)
    except Exception:
        return


def _build_citations_from_recommendations(
    items: list[RepoRecommendation],
    source: str,
    max_items: int = 10,
) -> list[RecommendationCitation]:
    citations: list[RecommendationCitation] = []
    for item in items[:max_items]:
        link = str(item.repo_url or item.html_url or "").strip()
        if not link:
            continue
        citations.append(
            RecommendationCitation(
                id=str(item.id or item.full_name or ""),
                source=source,
                title=str(item.full_name or item.id or ""),
                url=link,
                snippet=str(item.description or "")[:180] or None,
                score=int(item.match_score or 0),
                reason=(str(item.match_reasons[0]) if item.match_reasons else None),
            )
        )
    return citations


def _merge_citations(
    primary: list[RecommendationCitation],
    secondary: list[RecommendationCitation],
    max_items: int = 14,
) -> list[RecommendationCitation]:
    merged: list[RecommendationCitation] = []
    seen: set[str] = set()
    for row in primary + secondary:
        key = f"{str(row.source or '').lower()}::{str(row.url or '').lower()}::{str(row.id or '').lower()}"
        if not key.strip(":") or key in seen:
            continue
        seen.add(key)
        merged.append(row)
        if len(merged) >= max_items:
            break
    return merged


def _fetch_external_recommendations(
    *,
    query: str,
    requirement_text: str,
    mode: str,
    top_k: int,
    query_intents: list[str],
    hard_intents: list[str],
    progress_callback: Optional[Callable[[str], None]] = None,
) -> _ExternalRecall:
    if not RECOMMEND_EXTERNAL_SOURCES_ENABLED:
        return _ExternalRecall([], [], [], None, [], [], [])

    try:
        response = recommend_repositories(
            query=query,
            requirement_text=requirement_text,
            mode=mode,
            # Pull a larger pool so keyword-first ranking still has enough room
            # after intent guardrails and deduplication.
            limit=max(top_k * 4, 30),
            progress_callback=progress_callback,
        )
    except Exception as exc:  # noqa: BLE001
        return _ExternalRecall([], [], [f"外部多源召回失败，已回退目录库：{exc}"], None, [], [], [])

    collected: list[RepoRecommendation] = []
    for item in response.recommendations:
        text = _build_external_match_text(item)
        hit_intents = _match_case_intents(text, query_intents)
        if any(intent not in hit_intents for intent in hard_intents):
            continue

        reasons = list(item.match_reasons or [])
        if hit_intents:
            labels = [INTENT_GROUP_LABELS.get(intent, intent) for intent in hit_intents]
            reasons.insert(0, f"命中语义意图：{', '.join(labels)}")
        item.match_reasons = _dedupe_keep_order_text(reasons)

        risk_notes = list(item.risk_notes or [])
        if query_intents and len(hit_intents) < max(1, len(query_intents) // 2):
            risk_notes.append("仅覆盖部分语义意图，建议人工复核。")
        item.risk_notes = _dedupe_keep_order_text(risk_notes)

        if not item.product_type:
            item.product_type = ProductType.OPEN_SOURCE.value
        if item.action is None:
            deploy_url = str(item.repo_url or item.html_url or "").strip() or None
            item.action = RecommendationAction(
                action_type=ProductActionType.ONE_CLICK_DEPLOY.value,
                label="一键部署",
                url=deploy_url,
                deploy_supported=bool(deploy_url),
                detail=f"来源：{str(item.source or 'external').strip() or 'external'}",
            )
        if not item.deployment_mode:
            item.deployment_mode = "deploy" if bool(item.action and item.action.deploy_supported) else "solution"
        collected.append(item)

    warnings = list(response.warnings or [])
    if response.recommendations and not collected:
        warnings.append("外部多源候选已按语义护栏过滤。")
    kept_ids = {str(item.id or "") for item in collected}
    external_citations = [
        row
        for row in (response.citations or [])
        if not kept_ids or str(row.id or "") in kept_ids
    ]
    return _ExternalRecall(
        recommendations=collected,
        sources=[str(source) for source in (response.sources or []) if str(source).strip()],
        warnings=warnings,
        deep_summary=response.deep_summary,
        insight_points=[str(item) for item in (response.insight_points or []) if str(item).strip()],
        citations=external_citations,
        trace_steps=[str(item) for item in (response.trace_steps or []) if str(item).strip()],
    )


def _recommendation_key(item: RepoRecommendation) -> str:
    source = str(item.source or "catalog_db").strip().lower()
    locator = str(item.repo_url or item.html_url or item.id or item.full_name or "").strip().lower()
    return f"{source}::{locator}"


def _merge_recommendations(
    catalog_items: list[RepoRecommendation],
    external_items: list[RepoRecommendation],
    top_k: int,
) -> list[RepoRecommendation]:
    merged: list[RepoRecommendation] = list(catalog_items)
    seen: set[str] = {_recommendation_key(item) for item in merged}
    for item in external_items:
        key = _recommendation_key(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    merged.sort(key=lambda item: int(getattr(item, "match_score", 0) or 0), reverse=True)
    return merged[:top_k]


def recommend_products(
    *,
    query: str,
    requirement_text: str,
    mode: str,
    limit: int,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> RecommendationResponse:
    query_text = str(query or "").strip() or str(requirement_text or "").strip()
    trace_steps: list[str] = []

    def _trace(message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        trace_steps.append(text)
        _emit_progress(progress_callback, text)

    _trace("解析业务需求并构建意图标签...")
    with session_scope() as session:
        repo = DecisionRepository(session)
        capabilities = repo.list_capabilities()
        requested_codes = _infer_requested_capability_codes(query_text, capabilities)
        query_intents = _infer_query_intents(query_text)
        hard_intents = _infer_hard_intents_from_llm(query_text, requirement_text, query_intents)
        all_cases = repo.list_active_cases()
        if not all_cases:
            return RecommendationResponse(
                request_id=f"decision-{int(_now_ts())}",
                query=query or None,
                mode=mode,
                generated_at=_now_ts(),
                requirement_excerpt=query_text[:200] or None,
                search_query=query_text[:120] or None,
                profile=RecommendationProfile(summary=query_text[:180] or None),
                warnings=["候选目录为空，请先初始化产品目录。"],
                sources=["catalog_db"],
                trace_steps=trace_steps,
                recommendations=[],
            )

        # Product requirement: keep result set >= 10 whenever candidates are available.
        top_k = max(10, min(int(limit or 10), 20))
        _trace(f"目录候选加载完成：{len(all_cases)} 条，开始混合评分。")
        scored = [_score_case(case, query_text, requested_codes, query_intents) for case in all_cases]
        scored, guardrail_warnings = _apply_intent_guardrails(scored, query_intents, hard_intents)
        if not scored:
            _trace("目录结果触发语义护栏，切换外部多源深度搜索。")
            external = _fetch_external_recommendations(
                query=query_text,
                requirement_text=requirement_text,
                mode=mode,
                top_k=top_k,
                query_intents=query_intents,
                hard_intents=hard_intents,
                progress_callback=progress_callback,
            )
            sources = ["catalog_db", "intent_guardrail"]
            for source in external.sources:
                if source not in sources:
                    sources.append(source)
            merged_trace = trace_steps + [step for step in external.trace_steps if step not in trace_steps]
            return RecommendationResponse(
                request_id=f"decision-{int(_now_ts())}",
                query=query or None,
                mode=mode,
                generated_at=_now_ts(),
                requirement_excerpt=query_text[:200] or None,
                search_query=query_text[:120] or None,
                profile=RecommendationProfile(
                    summary=f"已识别语义意图：{', '.join(query_intents)}",
                    keywords=_dedupe_keep_order(requested_codes + query_intents),
                    must_have=query_intents,
                    scenarios=_extract_terms(query_text)[:8],
                ),
                warnings=guardrail_warnings + external.warnings,
                sources=sources,
                deep_summary=external.deep_summary,
                insight_points=external.insight_points,
                trace_steps=merged_trace,
                citations=external.citations[:12],
                recommendations=external.recommendations[:top_k],
            )
        scored.sort(
            key=lambda row: (row.final_score, row.capability_match, row.popularity),
            reverse=True,
        )
        _trace("目录混合评分完成，执行语义重排与候选融合。")
        top_rows, ai_used, ai_warnings = _rerank_with_ai(
            scored=scored,
            query_text=query_text[:280],
            top_k=top_k,
        )

        recommendations: list[RepoRecommendation] = []
        for row in top_rows:
            case_codes = _case_capability_codes(row.case)
            breakdown = ScoreBreakdown(
                relevance=row.relevance,
                popularity=row.popularity,
                cost_bonus=row.cost_bonus,
                capability_match=row.capability_match,
                final_score=row.final_score,
            )
            repo.create_evaluation(
                case_id=row.case.id,
                query_text=query_text,
                requested_capabilities=requested_codes,
                relevance_score=row.relevance,
                popularity_score=row.popularity,
                cost_bonus_score=row.cost_bonus,
                capability_match_score=row.capability_match,
                final_score=row.final_score,
                breakdown=breakdown.model_dump(),
            )

            capabilities_view = [
                CapabilityTag(
                    code=str(link.capability.code or ""),
                    name=str(link.capability.name or ""),
                    weight=int(link.weight or 100),
                )
                for link in (row.case.capabilities or [])
                if link.capability
            ]
            action = _build_action(row.case)
            recommendations.append(
                RepoRecommendation(
                    id=row.case.id,
                    full_name=row.case.title,
                    html_url=action.url or str(row.case.official_url or row.case.repo_url or ""),
                    description=row.case.summary,
                    language="N/A",
                    topics=[item.code for item in capabilities_view],
                    stars=int(row.popularity * 100),
                    forks=0,
                    open_issues=0,
                    license=str((row.case.metadata_json or {}).get("license") or ""),
                    archived=False,
                    pushed_at=None,
                    updated_days=0,
                    match_score=row.final_score,
                    match_reasons=_build_reasons(row, requested_codes, case_codes, query_intents),
                    match_tags=[item.name for item in capabilities_view[:6]],
                    risk_notes=_build_risks(row.case, row, requested_codes, query_intents),
                    health=_build_health_card(row),
                    source="catalog_db",
                    product_type=row.case.product_type.value,
                    official_url=row.case.official_url,
                    repo_url=row.case.repo_url,
                    capabilities=capabilities_view,
                    score_breakdown=breakdown,
                    action=action,
                    deployment_mode="deploy" if action.deploy_supported else "solution",
                )
            )

        _trace("并发拉取外部多源候选（GitHub/Gitee/GitCode/模板）...")
        external = _fetch_external_recommendations(
            query=query_text,
            requirement_text=requirement_text,
            mode=mode,
            top_k=top_k,
            query_intents=query_intents,
            hard_intents=hard_intents,
            progress_callback=progress_callback,
        )
        recommendations = _merge_recommendations(recommendations, external.recommendations, top_k)

        profile = RecommendationProfile(
            summary=(
                f"已映射 {len(requested_codes)} 个能力标签、识别 {len(query_intents)} 个语义意图，"
                f"按混合权重排序{'并叠加 AI 语义重排' if ai_used else ''}。"
            ),
            keywords=_dedupe_keep_order(requested_codes + query_intents),
            must_have=_dedupe_keep_order(requested_codes + query_intents),
            scenarios=_extract_terms(query_text)[:8],
        )
        warnings: list[str] = list(guardrail_warnings)
        if not requested_codes and not query_intents:
            warnings.append("未识别到明确能力标签，当前以通用相关性排序。")
        warnings.extend(ai_warnings)
        warnings.extend(external.warnings)
        sources = ["catalog_db"]
        if query_intents:
            sources.append("intent_guardrail")
        if ai_used:
            sources.append("semantic_rerank")
        for source in external.sources:
            if source not in sources:
                sources.append(source)
        catalog_citations = _build_citations_from_recommendations(recommendations, source="catalog_db", max_items=10)
        citations = _merge_citations(external.citations, catalog_citations, max_items=14)
        insight_points = [str(item) for item in (external.insight_points or []) if str(item).strip()]
        deep_summary = external.deep_summary
        if mode.lower() == "deep" and not deep_summary:
            deep_summary = (
                f"目录库与多源检索已融合，输出 {len(recommendations)} 个候选，"
                "排序优先使用关键词命中并结合语义重排。"
            )
        for step in external.trace_steps:
            if step not in trace_steps:
                trace_steps.append(step)
        _trace(f"排序与融合完成，最终输出 {len(recommendations)} 条建议。")

        return RecommendationResponse(
            request_id=f"decision-{int(_now_ts())}",
            query=query or None,
            mode=mode,
            generated_at=_now_ts(),
            requirement_excerpt=query_text[:200] or None,
            search_query=query_text[:120] or None,
            profile=profile,
            warnings=warnings,
            sources=sources,
            deep_summary=deep_summary,
            insight_points=insight_points,
            trace_steps=trace_steps,
            citations=citations,
            recommendations=recommendations,
        )


def resolve_product_action(*, case_id: str) -> RecommendationAction:
    with session_scope() as session:
        repo = DecisionRepository(session)
        case = repo.get_case_by_id(case_id)
        if not case:
            raise ValueError(f"product not found: {case_id}")
        return _build_action(case)


def seed_default_catalog() -> None:
    capabilities_seed: list[dict[str, Any]] = [
        {"code": "payment_gateway", "name": "支付网关", "domain": "finance"},
        {"code": "points_management", "name": "积分管理", "domain": "growth"},
        {"code": "coupon_management", "name": "卡券系统", "domain": "growth"},
        {"code": "audit_log", "name": "审计日志", "domain": "compliance"},
        {"code": "merchant_portal", "name": "商户端", "domain": "portal"},
        {"code": "resident_portal", "name": "居民端", "domain": "portal"},
        {"code": "government_subsidy", "name": "政府补贴资金池", "domain": "gov"},
        {"code": "split_settlement", "name": "多方分账", "domain": "finance"},
        {"code": "inventory_sku", "name": "SPU/SKU与库存", "domain": "commerce"},
        {"code": "cms_builder", "name": "CMS与页面装修", "domain": "content"},
        {"code": "community_portal", "name": "社区门户", "domain": "community"},
        {"code": "community_moderation", "name": "社区内容治理", "domain": "community"},
        {"code": "rbac", "name": "RBAC权限", "domain": "security"},
    ]

    with session_scope() as session:
        repo = DecisionRepository(session)
        for item in capabilities_seed:
            code = str(item["code"])
            repo.upsert_capability(
                code=code,
                name=str(item["name"]),
                description=f"{item['name']} capability",
                aliases=DEFAULT_CAPABILITY_ALIASES.get(code, []),
                domain=str(item["domain"]),
                active=True,
            )

        repo.upsert_case(
            slug="discourse",
            title="Discourse (Open Source Community Forum)",
            product_type=ProductType.OPEN_SOURCE,
            action_type=ProductActionType.ONE_CLICK_DEPLOY,
            summary="开源社区论坛平台，支持话题讨论、用户分层、内容审核与插件扩展。",
            official_url="https://www.discourse.org",
            repo_url="https://github.com/discourse/discourse",
            vendor="Discourse",
            pricing_model="open_source",
            estimated_monthly_cost_cents=0,
            popularity_score=86,
            metadata_json={"license": "GPL-2.0"},
            capability_codes=[
                "community_portal",
                "community_moderation",
                "resident_portal",
                "cms_builder",
                "rbac",
            ],
        )

        repo.upsert_case(
            slug="nodebb",
            title="NodeBB (Open Source Community Platform)",
            product_type=ProductType.OPEN_SOURCE,
            action_type=ProductActionType.ONE_CLICK_DEPLOY,
            summary="开源社区系统，适合搭建数字化社群、帖子互动与插件化运营。",
            official_url="https://nodebb.org",
            repo_url="https://github.com/NodeBB/NodeBB",
            vendor="NodeBB",
            pricing_model="open_source",
            estimated_monthly_cost_cents=0,
            popularity_score=80,
            metadata_json={"license": "GPL-3.0"},
            capability_codes=[
                "community_portal",
                "community_moderation",
                "resident_portal",
                "rbac",
            ],
        )

        repo.upsert_case(
            slug="odoo",
            title="Odoo (Open Source Suite)",
            product_type=ProductType.OPEN_SOURCE,
            action_type=ProductActionType.ONE_CLICK_DEPLOY,
            summary="开源 ERP/CRM/电商套件，可二次开发并支持私有化部署。",
            official_url="https://www.odoo.com",
            repo_url="https://github.com/odoo/odoo",
            vendor="Odoo",
            pricing_model="open_source",
            estimated_monthly_cost_cents=0,
            popularity_score=88,
            metadata_json={"license": "LGPL-3.0"},
            capability_codes=[
                "payment_gateway",
                "points_management",
                "coupon_management",
                "audit_log",
                "merchant_portal",
                "inventory_sku",
                "rbac",
            ],
        )

        repo.upsert_case(
            slug="erpnext",
            title="ERPNext (Open Source ERP)",
            product_type=ProductType.OPEN_SOURCE,
            action_type=ProductActionType.ONE_CLICK_DEPLOY,
            summary="开源 ERP，覆盖门店、库存、财务与权限管理场景。",
            official_url="https://erpnext.com",
            repo_url="https://github.com/frappe/erpnext",
            vendor="Frappe",
            pricing_model="open_source",
            estimated_monthly_cost_cents=0,
            popularity_score=82,
            metadata_json={"license": "GPL-3.0"},
            capability_codes=[
                "payment_gateway",
                "audit_log",
                "merchant_portal",
                "inventory_sku",
                "rbac",
            ],
        )

        repo.upsert_case(
            slug="circle-community",
            title="Circle.so Community Platform",
            product_type=ProductType.COMMERCIAL,
            action_type=ProductActionType.VISIT_OFFICIAL_SITE,
            summary="商业化社区 SaaS，适合数字化社群运营、会员分层与内容互动。",
            official_url="https://circle.so",
            repo_url=None,
            vendor="Circle",
            pricing_model="subscription",
            estimated_monthly_cost_cents=1200_00,
            popularity_score=78,
            capability_codes=[
                "community_portal",
                "community_moderation",
                "resident_portal",
                "cms_builder",
                "rbac",
            ],
        )

        repo.upsert_case(
            slug="salesforce",
            title="Salesforce Commerce/Service Cloud",
            product_type=ProductType.COMMERCIAL,
            action_type=ProductActionType.VISIT_OFFICIAL_SITE,
            summary="商业 SaaS 套件，覆盖商户运营、客户管理、流程自动化与审计能力。",
            official_url="https://www.salesforce.com",
            repo_url=None,
            vendor="Salesforce",
            pricing_model="subscription",
            estimated_monthly_cost_cents=3000_00,
            popularity_score=96,
            capability_codes=[
                "payment_gateway",
                "coupon_management",
                "audit_log",
                "merchant_portal",
                "cms_builder",
                "rbac",
            ],
        )

        repo.upsert_case(
            slug="hubspot",
            title="HubSpot CRM + CMS Hub",
            product_type=ProductType.COMMERCIAL,
            action_type=ProductActionType.VISIT_OFFICIAL_SITE,
            summary="商业 SaaS，适合营销运营、内容管理与线索转化。",
            official_url="https://www.hubspot.com",
            repo_url=None,
            vendor="HubSpot",
            pricing_model="subscription",
            estimated_monthly_cost_cents=1500_00,
            popularity_score=90,
            capability_codes=[
                "coupon_management",
                "merchant_portal",
                "cms_builder",
                "rbac",
            ],
        )
