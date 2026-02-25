import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from analyze.llm import LLMAdapter, LLMError, get_default_llm
from analyze.signals import sanitize_text, to_plain_text
from evidence import evidence_strength_rank, make_evidence, validate_evidence


@dataclass
class ProductStoryOutcome:
    story: Dict[str, Any]
    source: str
    error: Optional[str] = None


def _parse_json_object(raw: str) -> Dict[str, Any]:
    if not raw:
        raise LLMError("LLM returned empty output")
    text = raw.strip()
    obj: Optional[Dict[str, Any]] = None
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
    if obj is None:
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            try:
                obj = json.loads(text[brace_start : brace_end + 1])
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        raise LLMError("LLM response missing JSON object")
    return obj


def _first_sentence(text: str) -> str:
    cleaned = to_plain_text(text or "").strip()
    if not cleaned:
        return ""
    segments = [
        item.strip()
        for item in re.split(r"[。.!?！？\n]+", cleaned)
        if item and item.strip()
    ]
    if not segments:
        return cleaned[:120]
    for segment in segments:
        if not _looks_like_language_switch(segment):
            return segment[:120]
    return segments[0][:120]


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _looks_like_language_switch(text: str) -> bool:
    value = sanitize_text(text or "").strip()
    if not value:
        return True
    normalized = value.lower().strip()
    language_labels = {
        "english",
        "简体中文",
        "español",
        "hindi",
        "हिन्दी",
        "português",
        "日本語",
        "русский",
        "한국어",
        "français",
        "deutsch",
    }
    compact = re.sub(r"[\s|/,_-]+", "", normalized)
    compact_labels = {re.sub(r"[\s|/,_-]+", "", item) for item in language_labels}
    if compact in compact_labels:
        return True
    if value.count("|") >= 2:
        return True
    marker_pattern = re.compile(
        r"(english|español|português|русский|한국어|日本語|简体中文|hindi|हिन्दी|docs/i18n|readme)",
        re.IGNORECASE,
    )
    if marker_pattern.search(value) and "|" in value:
        return True
    words = [item for item in re.split(r"\s+", value) if item]
    if len(words) >= 3:
        latin_like = sum(1 for word in words if re.fullmatch(r"[A-Za-zÀ-ÿ._/-]+", word))
        if latin_like >= max(2, len(words) - 1):
            return True
    pure_words = [re.sub(r"[|/,_-]+", "", item.lower()) for item in words if item.strip()]
    if pure_words and all(word in compact_labels or word in language_labels for word in pure_words):
        return True
    return False


def _ensure_cn_claim(
    claim: Optional[Dict[str, Any]],
    fallback: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not claim:
        return None
    if _contains_cjk(claim.get("claim") or ""):
        return claim
    return fallback or claim


def _ensure_cn_list(
    claims: List[Dict[str, Any]],
    fallback: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    updated: List[Dict[str, Any]] = []
    for idx, claim in enumerate(claims):
        fallback_item = fallback[idx] if idx < len(fallback) else None
        picked = _ensure_cn_claim(claim, fallback_item)
        if picked is not None:
            updated.append(picked)
    return updated


def _ensure_cn_story(story: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    hook = story.get("hook") or {}
    fallback_hook = fallback.get("hook") or {}
    problem = story.get("problem_context") or {}
    fallback_problem = fallback.get("problem_context") or {}
    next_step = story.get("next_step_guidance") or {}
    fallback_next = fallback.get("next_step_guidance") or {}
    story["hook"] = {
        "headline": _ensure_cn_claim(hook.get("headline"), fallback_hook.get("headline")),
        "subline": _ensure_cn_claim(hook.get("subline"), fallback_hook.get("subline")),
    }
    story["problem_context"] = {
        "target_user": _ensure_cn_claim(problem.get("target_user"), fallback_problem.get("target_user")),
        "pain_point": _ensure_cn_claim(problem.get("pain_point"), fallback_problem.get("pain_point")),
        "current_bad_solution": _ensure_cn_claim(
            problem.get("current_bad_solution"),
            fallback_problem.get("current_bad_solution"),
        ),
    }
    story["what_this_repo_gives_you"] = _ensure_cn_list(
        story.get("what_this_repo_gives_you") or [],
        fallback.get("what_this_repo_gives_you") or [],
    )
    story["usage_scenarios"] = _ensure_cn_list(
        story.get("usage_scenarios") or [],
        fallback.get("usage_scenarios") or [],
    )
    story["why_it_matters_now"] = _ensure_cn_claim(
        story.get("why_it_matters_now"),
        fallback.get("why_it_matters_now"),
    )
    story["next_step_guidance"] = {
        "if_you_are_a_builder": _ensure_cn_claim(
            next_step.get("if_you_are_a_builder"),
            fallback_next.get("if_you_are_a_builder"),
        ),
        "if_you_are_a_pm_or_founder": _ensure_cn_claim(
            next_step.get("if_you_are_a_pm_or_founder"),
            fallback_next.get("if_you_are_a_pm_or_founder"),
        ),
        "if_you_are_evaluating": _ensure_cn_claim(
            next_step.get("if_you_are_evaluating"),
            fallback_next.get("if_you_are_evaluating"),
        ),
    }
    return story


def _derive_project_name(repo_index: Dict[str, Any]) -> str:
    name = str(repo_index.get("repo_name") or "").strip()
    return sanitize_text(name) or "该项目"


def _build_scope_summary(repo_index: Dict[str, Any], spotlights: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    tree = repo_index.get("tree") or {}
    languages = repo_index.get("languages") or []
    dependencies = repo_index.get("dependencies") or {}
    entrypoints = repo_index.get("entrypoints") or []
    repo_meta = repo_index.get("repo_meta") or {}
    python_deps = dependencies.get("python") or []
    node_deps = dependencies.get("node") or []
    return {
        "tree_entries": tree.get("count") or len(tree.get("entries") or []),
        "top_languages": [item.get("name") for item in languages[:4] if isinstance(item, dict) and item.get("name")],
        "dependency_counts": {
            "python": len(python_deps),
            "node": len(node_deps),
        },
        "entrypoints_count": len(entrypoints) if isinstance(entrypoints, list) else 0,
        "spotlights_count": len((spotlights or {}).get("items") or []) if isinstance(spotlights, dict) else 0,
        "stars": repo_meta.get("stars"),
        "forks": repo_meta.get("forks"),
    }


def _build_evidence_catalog(repo_index: Dict[str, Any], spotlights: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []

    readme_summary = repo_index.get("readme_summary") or {}
    readme_path = readme_summary.get("path")
    readme_line_range = readme_summary.get("line_range")
    if readme_path and readme_line_range:
        evidence.append(
            make_evidence(
                "readme",
                [{"kind": "readme", "file": readme_path, "line_range": readme_line_range}],
                "readme_summary",
                "medium",
            )
        )

    tree_entries = (repo_index.get("tree") or {}).get("entries") or []
    if tree_entries:
        sources = []
        for entry in tree_entries[:8]:
            if isinstance(entry, str):
                sources.append({"kind": "structure", "file": entry.strip().rstrip("/")})
        if sources:
            evidence.append(make_evidence("structure", sources, "repo_tree_entries", "medium"))

    dependencies = repo_index.get("dependencies") or {}
    dep_sources: List[Dict[str, Any]] = []
    for item in dependencies.get("python") or []:
        if isinstance(item, dict) and item.get("name"):
            dep_sources.append(
                {"kind": "dependency", "file": item.get("source"), "symbol": item.get("name")}
            )
    for item in dependencies.get("node") or []:
        if isinstance(item, dict) and item.get("name"):
            dep_sources.append(
                {"kind": "dependency", "file": item.get("source"), "symbol": item.get("name")}
            )
    if dep_sources:
        evidence.append(make_evidence("dependency", dep_sources, "dependency_files", "medium"))

    config_files = repo_index.get("config_files") or []
    if config_files:
        sources = [{"kind": "config", "file": name} for name in config_files[:3]]
        evidence.append(make_evidence("config", sources, "config_files", "weak"))

    spotlight_items = (spotlights or {}).get("items") if isinstance(spotlights, dict) else []
    if isinstance(spotlight_items, list):
        for item in spotlight_items:
            if isinstance(item, dict) and validate_evidence(item.get("evidence") or {}):
                evidence.append(item["evidence"])
                break

    return evidence


def _claim(claim: str, evidence: Optional[Dict[str, Any]], confidence: float) -> Optional[Dict[str, Any]]:
    if not evidence or not validate_evidence(evidence):
        return None
    value = to_plain_text(claim or "").strip()
    if not value:
        return None
    clamped = max(0.0, min(1.0, float(confidence)))
    return {"claim": value, "evidence": evidence, "confidence": clamped}


def _pick_evidence(evidence_by_type: Dict[str, List[Dict[str, Any]]], preferred: List[str]) -> Optional[Dict[str, Any]]:
    for key in preferred:
        items = evidence_by_type.get(key) or []
        if items:
            return items[0]
    return None


def _fallback_product_story(
    repo_index: Dict[str, Any],
    readme_excerpt: str,
    evidence_catalog: List[Dict[str, Any]],
) -> Dict[str, Any]:
    project = _derive_project_name(repo_index)
    evidence_by_type: Dict[str, List[Dict[str, Any]]] = {}
    for item in evidence_catalog:
        evidence_by_type.setdefault(item.get("type"), []).append(item)
    for items in evidence_by_type.values():
        items.sort(key=lambda ev: evidence_strength_rank(ev.get("strength")), reverse=True)

    readme_evidence = _pick_evidence(evidence_by_type, ["readme", "structure"])
    structure_evidence = _pick_evidence(evidence_by_type, ["structure"])
    dependency_evidence = _pick_evidence(evidence_by_type, ["dependency"])
    code_evidence = _pick_evidence(evidence_by_type, ["code"])

    headline = ""
    if _contains_cjk(readme_excerpt):
        candidate = _first_sentence(readme_excerpt)
        if candidate and _contains_cjk(candidate) and not _looks_like_language_switch(candidate):
            headline = candidate
    if not headline:
        headline = f"{project}帮助你在不读代码的情况下快速判断价值。"

    hook_headline = _claim(headline, readme_evidence, 0.5)
    hook_subline = _claim(
        "适合需要快速判断项目是否值得深入了解的人。",
        readme_evidence,
        0.45,
    )

    problem_target = _claim(
        "需要在不深入技术细节的情况下评估价值的人。",
        readme_evidence or structure_evidence,
        0.4,
    )
    problem_pain = _claim(
        "不读代码或长文档就难以看清项目意图。",
        readme_evidence or structure_evidence,
        0.4,
    )
    problem_bad = _claim(
        "多数人只能快速略读或猜测，仍然不踏实。",
        readme_evidence or structure_evidence,
        0.35,
    )

    outcomes: List[Dict[str, Any]] = []
    outcomes.append(
        _claim(
            "更快做出清晰判断",
            readme_evidence or structure_evidence,
            0.4,
        )
    )
    outcomes.append(
        _claim(
            "减少对是否匹配的猜测",
            dependency_evidence or structure_evidence,
            0.35,
        )
    )
    outcomes.append(
        _claim(
            "形成团队一致的价值叙事",
            code_evidence or structure_evidence,
            0.3,
        )
    )
    outcomes = [item for item in outcomes if item]

    scenarios: List[Dict[str, Any]] = []
    scenarios.append(
        _claim(
            "在决定这个项目是否适合团队需求时。",
            readme_evidence or structure_evidence,
            0.4,
        )
    )
    scenarios.append(
        _claim(
            "在深入前需要一个简明解释时。",
            readme_evidence or structure_evidence,
            0.35,
        )
    )
    scenarios = [item for item in scenarios if item]

    why_now = _claim(
        "团队推进节奏快，投入时间前的清晰判断更重要。",
        readme_evidence or structure_evidence,
        0.35,
    )

    next_builder = _claim(
        "先确认目标是否与你想要的结果一致。",
        readme_evidence or structure_evidence,
        0.3,
    )
    next_pm = _claim(
        "判断是否与你的路线图或当前优先级一致。",
        readme_evidence or structure_evidence,
        0.3,
    )
    next_eval = _claim(
        "先明确你期待的价值，再验证该项目是否支持。",
        readme_evidence or structure_evidence,
        0.3,
    )

    return {
        "hook": {"headline": hook_headline, "subline": hook_subline},
        "problem_context": {
            "target_user": problem_target,
            "pain_point": problem_pain,
            "current_bad_solution": problem_bad,
        },
        "what_this_repo_gives_you": outcomes,
        "usage_scenarios": scenarios,
        "why_it_matters_now": why_now,
        "next_step_guidance": {
            "if_you_are_a_builder": next_builder,
            "if_you_are_a_pm_or_founder": next_pm,
            "if_you_are_evaluating": next_eval,
        },
    }


def _evidence_briefs(evidence_catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    briefs: List[Dict[str, Any]] = []
    for item in evidence_catalog:
        if not validate_evidence(item):
            continue
        sources = item.get("sources") or []
        summary_parts: List[str] = []
        for source in sources[:3]:
            file = source.get("file") or source.get("section") or source.get("symbol")
            if file:
                summary_parts.append(str(file))
        brief = {
            "id": item.get("id"),
            "type": item.get("type"),
            "strength": item.get("strength"),
            "summary": ", ".join(summary_parts) if summary_parts else item.get("type"),
        }
        briefs.append(brief)
    return briefs


def _resolve_claim(
    raw: Dict[str, Any],
    evidence_by_id: Dict[str, Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    label: str,
) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        skipped.append({"label": label, "reason": "invalid_claim"})
        return None
    claim_text = sanitize_text(str(raw.get("claim") or "")).strip()
    evidence_id = raw.get("evidence_id")
    confidence = raw.get("confidence")
    if not claim_text or not evidence_id:
        skipped.append({"label": label, "reason": "missing_claim_or_evidence"})
        return None
    evidence = evidence_by_id.get(str(evidence_id))
    if not evidence:
        skipped.append({"label": label, "reason": "evidence_not_found"})
        return None
    try:
        confidence_value = float(confidence)
    except Exception:
        confidence_value = 0.4
    return _claim(claim_text, evidence, confidence_value)


def _normalize_story(
    raw: Dict[str, Any],
    evidence_catalog: List[Dict[str, Any]],
) -> Dict[str, Any]:
    evidence_by_id = {item["id"]: item for item in evidence_catalog if validate_evidence(item)}
    skipped: List[Dict[str, Any]] = []

    hook_raw = raw.get("hook") or {}
    problem_raw = raw.get("problem_context") or {}
    next_raw = raw.get("next_step_guidance") or {}

    hook = {
        "headline": _resolve_claim(hook_raw.get("headline") or {}, evidence_by_id, skipped, "hook.headline"),
        "subline": _resolve_claim(hook_raw.get("subline") or {}, evidence_by_id, skipped, "hook.subline"),
    }

    problem_context = {
        "target_user": _resolve_claim(problem_raw.get("target_user") or {}, evidence_by_id, skipped, "problem.target_user"),
        "pain_point": _resolve_claim(problem_raw.get("pain_point") or {}, evidence_by_id, skipped, "problem.pain_point"),
        "current_bad_solution": _resolve_claim(
            problem_raw.get("current_bad_solution") or {},
            evidence_by_id,
            skipped,
            "problem.current_bad_solution",
        ),
    }

    outcomes: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw.get("what_this_repo_gives_you") or []):
        resolved = _resolve_claim(item, evidence_by_id, skipped, f"outcome[{idx}]")
        if resolved:
            outcomes.append(resolved)

    scenarios: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw.get("usage_scenarios") or []):
        resolved = _resolve_claim(item, evidence_by_id, skipped, f"scenario[{idx}]")
        if resolved:
            scenarios.append(resolved)

    why_now = _resolve_claim(raw.get("why_it_matters_now") or {}, evidence_by_id, skipped, "why_it_matters_now")

    next_step_guidance = {
        "if_you_are_a_builder": _resolve_claim(
            next_raw.get("if_you_are_a_builder") or {}, evidence_by_id, skipped, "next.builder"
        ),
        "if_you_are_a_pm_or_founder": _resolve_claim(
            next_raw.get("if_you_are_a_pm_or_founder") or {}, evidence_by_id, skipped, "next.pm"
        ),
        "if_you_are_evaluating": _resolve_claim(
            next_raw.get("if_you_are_evaluating") or {}, evidence_by_id, skipped, "next.eval"
        ),
    }

    return {
        "hook": hook,
        "problem_context": problem_context,
        "what_this_repo_gives_you": outcomes,
        "usage_scenarios": scenarios,
        "why_it_matters_now": why_now,
        "next_step_guidance": next_step_guidance,
        "meta": {"skipped_claims": skipped},
    }


def _fallback_reason_code(exc: Exception) -> str:
    text = str(exc or "").strip()
    lowered = text.lower()
    if "openai_api_key" in lowered and "missing" in lowered:
        return "OPENAI_API_KEY_MISSING"
    if "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        return "RATE_LIMIT"
    if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
        return "AUTH_FAILED"
    if "timeout" in lowered or "timed out" in lowered:
        return "TIMEOUT"
    if (
        "parse failed" in lowered
        or "missing json object" in lowered
        or "missing choices" in lowered
        or "missing content" in lowered
        or "invalid json" in lowered
    ):
        return "INVALID_RESPONSE"
    if isinstance(exc, LLMError):
        return "LLM_ERROR"
    return "UNEXPECTED_ERROR"


def build_product_story(
    repo_index: Dict[str, Any],
    readme_excerpt: str,
    spotlights: Optional[Dict[str, Any]],
    log: Callable[[str], None],
    llm: Optional[LLMAdapter] = None,
) -> ProductStoryOutcome:
    evidence_catalog = _build_evidence_catalog(repo_index, spotlights)
    evidence_catalog = [item for item in evidence_catalog if validate_evidence(item)]
    readme_excerpt = to_plain_text(readme_excerpt or "").strip()
    project = _derive_project_name(repo_index)
    scope_summary = _build_scope_summary(repo_index, spotlights)
    evidence_briefs = _evidence_briefs(evidence_catalog)

    payload = {
        "project_name": project,
        "readme_excerpt": readme_excerpt[:4000],
        "scope_summary": scope_summary,
        "evidence": evidence_briefs,
    }
    adapter = llm or get_default_llm()
    try:
        log("[visualize] analyze_product_story")
        raw = adapter.generate_product_story(payload)
        parsed = _parse_json_object(raw)
        story = _normalize_story(parsed, evidence_catalog)
        fallback_story = _fallback_product_story(repo_index, readme_excerpt, evidence_catalog)
        story = _ensure_cn_story(story, fallback_story)
        story["meta"]["source"] = "llm"
        story["meta"]["evidence_catalog"] = evidence_catalog
        log("[visualize] product_story source=llm")
        return ProductStoryOutcome(story=story, source="llm", error=None)
    except Exception as exc:  # noqa: BLE001
        log(f"[visualize] product_story fallback reason={exc}")
        story = _fallback_product_story(repo_index, readme_excerpt, evidence_catalog)
        story["meta"] = {
            "source": "fallback",
            "reason_code": _fallback_reason_code(exc),
            "error": str(exc),
            "evidence_catalog": evidence_catalog,
        }
        return ProductStoryOutcome(story=story, source="fallback", error=str(exc))
