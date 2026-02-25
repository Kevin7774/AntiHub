import hashlib
import json
from typing import Any, Dict, List, Optional

EVIDENCE_TYPES = {"code", "structure", "dependency", "readme", "config", "call_graph"}
EVIDENCE_STRENGTH = {"strong", "medium", "weak"}


def _normalize_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        cleaned = {}
        for key, value in source.items():
            if value is None:
                continue
            cleaned[key] = value
        normalized.append(cleaned)
    return normalized


def build_evidence_id(evidence_type: str, sources: List[Dict[str, Any]], derivation_rule: str) -> str:
    payload = {
        "type": evidence_type,
        "sources": _normalize_sources(sources),
        "derivation_rule": derivation_rule or "",
    }
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"ev_{digest[:12]}"


def make_evidence(
    evidence_type: str,
    sources: List[Dict[str, Any]],
    derivation_rule: str,
    strength: str,
) -> Dict[str, Any]:
    evidence_type = str(evidence_type or "").strip()
    if evidence_type not in EVIDENCE_TYPES:
        evidence_type = "structure"
    strength = str(strength or "").strip().lower()
    if strength not in EVIDENCE_STRENGTH:
        strength = "weak"
    sources = _normalize_sources(sources)
    evidence_id = build_evidence_id(evidence_type, sources, derivation_rule)
    return {
        "id": evidence_id,
        "type": evidence_type,
        "sources": sources,
        "derivation_rule": derivation_rule,
        "strength": strength,
    }


def validate_evidence(evidence: Dict[str, Any]) -> bool:
    if not isinstance(evidence, dict):
        return False
    if not evidence.get("id"):
        return False
    if evidence.get("type") not in EVIDENCE_TYPES:
        return False
    if evidence.get("strength") not in EVIDENCE_STRENGTH:
        return False
    sources = evidence.get("sources")
    if not isinstance(sources, list) or not sources:
        return False
    if not evidence.get("derivation_rule"):
        return False
    return True


def evidence_strength_rank(strength: Optional[str]) -> int:
    order = {"weak": 0, "medium": 1, "strong": 2}
    return order.get(str(strength or "").lower(), 0)
