import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from analyze.llm import LLMAdapter, LLMError, get_default_llm
from analyze.mermaid import validate_mermaid
from analyze.report_store import ReportStore
from analyze.signals import extract_signals, sanitize_text
from config import ANALYZE_ROOT, BUILD_ROOT, MERMAID_VALIDATE_RETRIES
from git_ops import GitRefNotFoundError, clone_repo, normalize_ref

BUSINESS_GOAL_KEYWORDS = (
    "goal",
    "goals",
    "objective",
    "objectives",
    "purpose",
    "mission",
    "目标",
    "目的",
    "愿景",
)

BUSINESS_FEATURE_KEYWORDS = (
    "feature",
    "features",
    "capability",
    "capabilities",
    "highlights",
    "功能",
    "特性",
    "亮点",
)


@dataclass
class AnalysisOutcome:
    report: Dict[str, Any]
    cache_hit: bool
    commit_sha: str
    signals_path: Optional[str]


class AnalysisFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _strip_fences(code: str) -> str:
    stripped = code.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```\w*", "", stripped)
        stripped = stripped.strip()
        if stripped.endswith("```"):
            stripped = stripped[: -3].strip()
    return stripped


def _extract_mermaid_blocks(markdown: str) -> Tuple[str, List[str]]:
    pattern = re.compile(r"```mermaid\s*(.*?)```", re.DOTALL | re.IGNORECASE)
    mermaids = [match.group(1).strip() for match in pattern.finditer(markdown)]
    cleaned = pattern.sub("", markdown)
    return cleaned.strip(), mermaids


def _parse_llm_output(raw: str) -> Tuple[str, List[str]]:
    if not raw:
        raise LLMError("LLM returned empty output")
    text = raw.strip()
    obj = None
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
    markdown = None
    mermaids: List[str] = []
    if isinstance(obj, dict):
        markdown = obj.get("markdown")
        mermaids = obj.get("mermaids") or []
    if not markdown:
        markdown = text
    cleaned_markdown, embedded_mermaids = _extract_mermaid_blocks(markdown)
    merged_mermaids: List[str] = []
    for item in mermaids or []:
        code = _strip_fences(str(item))
        if code:
            merged_mermaids.append(code)
    for item in embedded_mermaids:
        code = _strip_fences(item)
        if code and code not in merged_mermaids:
            merged_mermaids.append(code)
    return cleaned_markdown, merged_mermaids


def _normalize_heading(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", " ", (text or "").lower())
    return cleaned.strip()


def _extract_section_items(lines: List[str]) -> List[str]:
    items: List[str] = []
    paragraphs: List[str] = []
    in_code = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if item:
                items.append(item)
            continue
        if re.match(r"\d+\.\s+", line):
            item = re.sub(r"^\d+\.\s+", "", line).strip()
            if item:
                items.append(item)
            continue
        if line.startswith("!"):
            continue
        if line.startswith("[") and "]" in line and "(" in line:
            paragraphs.append(line)
            continue
        paragraphs.append(line)
    if items:
        return items
    for paragraph in paragraphs:
        if paragraph:
            items.append(paragraph)
        if len(items) >= 5:
            break
    return items


def _extract_readme_marketing_sections(readme_text: str) -> Dict[str, List[str]]:
    if not readme_text:
        return {"goals": [], "features": []}
    sections: List[Tuple[str, List[str]]] = []
    current_heading: Optional[str] = None
    current_lines: List[str] = []
    for raw in readme_text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            if current_heading:
                sections.append((current_heading, current_lines))
            current_heading = line.lstrip("#").strip()
            current_lines = []
            continue
        if current_heading:
            current_lines.append(line)
    if current_heading:
        sections.append((current_heading, current_lines))

    goals: List[str] = []
    features: List[str] = []
    for heading, lines in sections:
        normalized = _normalize_heading(heading)
        if any(keyword in normalized for keyword in BUSINESS_GOAL_KEYWORDS):
            goals.extend(_extract_section_items(lines))
        if any(keyword in normalized for keyword in BUSINESS_FEATURE_KEYWORDS):
            features.extend(_extract_section_items(lines))
    return {
        "goals": [item for item in goals if item][:8],
        "features": [item for item in features if item][:8],
    }


def _parse_business_output(raw: str) -> Dict[str, Any]:
    if not raw:
        raise LLMError("LLM returned empty output")
    text = raw.strip()
    obj = None
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


def _sanitize_list(items: Iterable[Any], limit: int) -> List[str]:
    cleaned: List[str] = []
    for item in items:
        value = sanitize_text(str(item or "")).strip()
        if not value:
            continue
        cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def _ensure_min_items(items: List[str], minimum: int, fallback: List[str]) -> List[str]:
    if len(items) >= minimum:
        return items[:minimum]
    needed = minimum - len(items)
    return (items + fallback[:needed])[:minimum]


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _strip_code_blocks(markdown: str) -> str:
    text = re.sub(r"```.*?```", "", markdown or "", flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", "", text)
    return text


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    total = cjk_count + latin_count
    if total == 0:
        return 0.0
    return cjk_count / total


def _should_translate_markdown(markdown: str) -> bool:
    stripped = _strip_code_blocks(markdown)
    if not stripped.strip():
        return False
    ratio = _cjk_ratio(stripped)
    return ratio < 0.3


def _ensure_cjk_text(value: str, fallback: str) -> Tuple[str, bool]:
    if _contains_cjk(value):
        return value, False
    return fallback, True


def _ensure_cjk_list(values: List[str], fallback: List[str]) -> Tuple[List[str], bool]:
    updated: List[str] = []
    changed = False
    for idx, item in enumerate(values):
        if _contains_cjk(item):
            updated.append(item)
            continue
        changed = True
        if idx < len(fallback):
            updated.append(fallback[idx])
        elif fallback:
            updated.append(fallback[-1])
        else:
            updated.append(item)
    return updated, changed


def _ensure_cjk_cards(cards: List[Dict[str, str]], fallback: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], bool]:
    updated: List[Dict[str, str]] = []
    changed = False
    for idx, item in enumerate(cards):
        title = item.get("title") or ""
        description = item.get("description") or ""
        if _contains_cjk(title) and _contains_cjk(description):
            updated.append(item)
            continue
        changed = True
        if idx < len(fallback):
            updated.append(fallback[idx])
    if not updated and fallback:
        changed = True
        updated = fallback
    return updated, changed


def _derive_project_name(repo_url: str, signals: Dict[str, Any]) -> str:
    readme = signals.get("readme") or {}
    headings = readme.get("headings") or []
    if headings:
        return str(headings[0]).strip()
    repo_hint = (repo_url or "").rstrip("/").split("/")[-1]
    if repo_hint.endswith(".git"):
        repo_hint = repo_hint[: -4]
    return repo_hint or "该项目"


def _fallback_business_summary(repo_url: str, signals: Dict[str, Any], readme_sections: Dict[str, List[str]]) -> Dict[str, Any]:
    project = sanitize_text(_derive_project_name(repo_url, signals))
    fallback_values = [
        f"帮助干系人更清楚地理解 {project} 的目标与价值。",
        "在投入时间前，帮助团队对预期结果达成一致。",
        "更容易向合作伙伴或客户说明这个项目的意义。",
    ]
    fallback_scenarios = [
        "当评估项目是否匹配业务需求时。",
        "当需要向非技术干系人介绍项目时。",
    ]
    cards: List[Dict[str, str]] = []
    source_hint = "inferred"
    goals = [item for item in (readme_sections.get("goals") or []) if _contains_cjk(item)]
    features = [item for item in (readme_sections.get("features") or []) if _contains_cjk(item)]
    if goals or features:
        source_hint = "extracted"
        for item in (goals + features)[:4]:
            title = item.split(":")[0].split("。")[0].strip() or "重点"
            description = item
            cards.append({"title": sanitize_text(title), "description": sanitize_text(description)})
    if not cards:
        cards = [
            {
                "title": "价值主张",
                "description": f"{project} 旨在帮助团队更清楚地理解项目价值与适用场景。",
            }
        ]
    return {
        "slogan": sanitize_text(f"{project} 让团队更快达成价值共识。"),
        "business_values": [sanitize_text(item) for item in fallback_values],
        "business_scenarios": [sanitize_text(item) for item in fallback_scenarios],
        "readme_marketing_cards": cards,
        "source": "partial",
        "readme_cards_source": source_hint,
    }


def _normalize_business_summary(
    raw: Dict[str, Any],
    repo_url: str,
    signals: Dict[str, Any],
    readme_sections: Dict[str, List[str]],
    readme_cards_source: str,
) -> Dict[str, Any]:
    project = _derive_project_name(repo_url, signals)
    slogan = sanitize_text(str(raw.get("slogan") or "")).strip() or f"{project} 让团队更快达成价值共识。"
    business_values = _sanitize_list(raw.get("business_values") or [], 6)
    business_scenarios = _sanitize_list(raw.get("business_scenarios") or [], 4)
    cards_raw = raw.get("readme_marketing_cards") or []
    cards: List[Dict[str, str]] = []
    if isinstance(cards_raw, list):
        for item in cards_raw:
            if not isinstance(item, dict):
                continue
            title = sanitize_text(str(item.get("title") or "")).strip()
            description = sanitize_text(str(item.get("description") or "")).strip()
            if not title and description:
                title = description.split("。")[0].split(".")[0].strip()
            if not title or not description:
                continue
            cards.append({"title": title, "description": description})
            if len(cards) >= 5:
                break
    fallback = _fallback_business_summary(repo_url, signals, readme_sections)
    used_fallback = False
    if len(business_values) < 3:
        used_fallback = True
    if len(business_scenarios) < 2:
        used_fallback = True
    if not cards:
        used_fallback = True
    business_values = _ensure_min_items(business_values, 3, fallback["business_values"])
    business_scenarios = _ensure_min_items(business_scenarios, 2, fallback["business_scenarios"])
    if not cards:
        cards = fallback["readme_marketing_cards"]
    slogan, slogan_changed = _ensure_cjk_text(slogan, fallback["slogan"])
    business_values, values_changed = _ensure_cjk_list(business_values[:3], fallback["business_values"])
    business_scenarios, scenarios_changed = _ensure_cjk_list(business_scenarios[:2], fallback["business_scenarios"])
    cards, cards_changed = _ensure_cjk_cards(cards, fallback["readme_marketing_cards"])
    if slogan_changed or values_changed or scenarios_changed or cards_changed:
        used_fallback = True
    source_value = "extracted" if readme_cards_source == "extracted" else "inferred"
    if used_fallback:
        source_value = "partial"
    return {
        "slogan": slogan,
        "business_values": business_values[:3],
        "business_scenarios": business_scenarios[:2],
        "readme_marketing_cards": cards,
        "source": source_value,
        "readme_cards_source": readme_cards_source,
    }


def _build_business_summary(
    repo_url: str,
    signals: Dict[str, Any],
    llm: LLMAdapter,
    log: Callable[[str], None],
) -> Dict[str, Any]:
    readme_excerpt = str((signals.get("readme") or {}).get("excerpt") or "")
    readme_sections = _extract_readme_marketing_sections(readme_excerpt)
    has_sections = bool(readme_sections.get("goals") or readme_sections.get("features"))
    readme_cards_source = "extracted" if has_sections else "inferred"
    input_payload = {
        "repo_url": repo_url,
        "project_name": _derive_project_name(repo_url, signals),
        "readme_sections": readme_sections,
        "readme_excerpt": readme_excerpt[:6000],
        "headings": (signals.get("readme") or {}).get("headings") or [],
        "bullets": (signals.get("readme") or {}).get("bullets") or [],
    }
    try:
        log("[analyze] analyze_business_value")
        raw_response = llm.generate_business_summary(input_payload)
        parsed = _parse_business_output(raw_response)
        summary = _normalize_business_summary(
            parsed,
            repo_url=repo_url,
            signals=signals,
            readme_sections=readme_sections,
            readme_cards_source=readme_cards_source,
        )
        log(f"[analyze] business_summary source={summary.get('source')} readme_cards_source={readme_cards_source}")
        return summary
    except Exception as exc:  # noqa: BLE001
        log(f"[analyze] business_summary fallback reason={exc}")
        fallback = _fallback_business_summary(repo_url, signals, readme_sections)
        log(
            f"[analyze] business_summary source={fallback.get('source')} readme_cards_source={fallback.get('readme_cards_source')}"
        )
        return fallback


def _default_mermaid(signals: Dict[str, Any]) -> str:
    repo_hint = "App"
    readme = signals.get("readme") or {}
    headings = readme.get("headings") or []
    if headings:
        repo_hint = str(headings[0]).strip() or repo_hint
    return f"""flowchart LR
  User[User] --> {repo_hint}[{repo_hint}]
  {repo_hint} --> Service[Services]
  Service --> Dependencies[Dependencies]
"""


def _compute_repo_fingerprint(repo_path: Path, limit: int = 2000) -> str:
    hasher = hashlib.sha1()
    count = 0
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "venv", ".venv"}]
        for name in sorted(files):
            if count >= limit:
                break
            path = Path(root) / name
            rel = str(path.relative_to(repo_path))
            try:
                stat = path.stat()
            except Exception:
                continue
            hasher.update(rel.encode("utf-8", errors="ignore"))
            hasher.update(str(stat.st_size).encode("utf-8"))
            hasher.update(str(int(stat.st_mtime)).encode("utf-8"))
            count += 1
        if count >= limit:
            break
    return hasher.hexdigest()


def _resolve_commit_sha(repo_path: Path) -> Optional[str]:
    try:
        sha = (
            subprocess.check_output(["git", "-C", str(repo_path), "rev-parse", "HEAD"], text=True)
            .strip()
        )
        return sha or None
    except Exception:
        return None


def _ensure_repo_path(
    case_id: str,
    repo_url: str,
    ref: Optional[str],
    preferred_path: Optional[Path],
    enable_submodules: bool,
    enable_lfs: bool,
    log: Callable[[str], None],
) -> Tuple[Path, bool, Optional[str]]:
    if preferred_path and preferred_path.exists():
        return preferred_path, False, None
    fallback_path = Path(BUILD_ROOT) / case_id
    if fallback_path.exists():
        return fallback_path, False, None
    target_dir = Path(ANALYZE_ROOT) / case_id
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    log(f"[analyze] cloning repo to {target_dir}")
    try:
        clone_result = clone_repo(repo_url, ref, target_dir, enable_submodules, enable_lfs)
    except GitRefNotFoundError as exc:
        raise AnalysisFailure("ANALYZE_REPO_NOT_FOUND", str(exc)) from exc
    except Exception as exc:
        raise AnalysisFailure("ANALYZE_REPO_NOT_FOUND", str(exc)) from exc
    return target_dir, True, clone_result.resolved_ref


def _validate_and_fix_mermaids(
    mermaids: List[str],
    llm: LLMAdapter,
    report_dir: Path,
) -> Tuple[List[str], List[Dict[str, Any]], List[str], bool]:
    validations: List[Dict[str, Any]] = []
    assets: List[str] = []
    fixed_any = False
    final_mermaids: List[str] = []

    for index, original in enumerate(mermaids):
        code = _strip_fences(original)
        attempts = 0
        fixed = False
        error_message = ""
        asset_path = None
        method = ""
        ok = False
        while attempts <= MERMAID_VALIDATE_RETRIES:
            ok, error_message, asset_path, method = validate_mermaid(code, report_dir, index)
            if ok:
                break
            if attempts >= MERMAID_VALIDATE_RETRIES:
                break
            try:
                repaired = llm.repair_mermaid(code, error_message)
            except Exception:
                repaired = ""
            repaired = _strip_fences(repaired)
            if repaired and repaired != code:
                code = repaired
                fixed = True
                fixed_any = True
            attempts += 1
        validation_item = {
            "index": index,
            "ok": ok,
            "method": method,
            "attempts": attempts + 1,
            "fixed": fixed,
            "error": None if ok else error_message,
        }
        if asset_path:
            validation_item["asset"] = Path(asset_path).name
            assets.append(Path(asset_path).name)
        validations.append(validation_item)
        final_mermaids.append(code)

    return final_mermaids, validations, assets, fixed_any


def _append_mermaid_failures(markdown: str, mermaids: List[str], validations: List[Dict[str, Any]]) -> str:
    failed = [item for item in validations if not item.get("ok")]
    if not failed:
        return markdown
    sections = ["\n\n## Mermaid 渲染失败", "以下 Mermaid 未通过校验，已保留原始代码："]
    for item in failed:
        index = item.get("index")
        error = item.get("error") or "unknown error"
        code = mermaids[index] if isinstance(index, int) and index < len(mermaids) else ""
        sections.append(f"\n### 图 {index + 1}\n失败原因：{error}\n")
        sections.append("```mermaid")
        sections.append(code)
        sections.append("```")
    return markdown + "\n".join(sections)


def _build_report_payload(
    case_id: str,
    repo_url: str,
    commit_sha: str,
    markdown: str,
    mermaids: List[str],
    assets: List[str],
    validations: List[Dict[str, Any]],
    signals_path: Optional[str],
    business_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary = {
        "total": len(validations),
        "failed": len([item for item in validations if not item.get("ok")]),
        "ok": all(item.get("ok") for item in validations) if validations else True,
    }
    validation_payload = {
        "summary": summary,
        "items": validations,
        "error_code": "ANALYZE_MERMAID_VALIDATE_FAILED" if summary["failed"] else None,
    }
    payload: Dict[str, Any] = {
        "case_id": case_id,
        "repo_url": repo_url,
        "commit_sha": commit_sha,
        "created_at": time.time(),
        "markdown": markdown,
        "mermaids": mermaids,
        "assets": assets,
        "validation": validation_payload,
    }
    if business_summary is not None:
        payload["business_summary"] = business_summary
    if signals_path:
        payload["signals_path"] = signals_path
    return payload


def _has_business_summary(report: Dict[str, Any]) -> bool:
    summary = report.get("business_summary") if isinstance(report, dict) else None
    if not isinstance(summary, dict):
        return False
    slogan = str(summary.get("slogan") or "").strip()
    values = summary.get("business_values") or []
    scenarios = summary.get("business_scenarios") or []
    return bool(slogan) and isinstance(values, list) and isinstance(scenarios, list)


def run_analysis(
    case_id: str,
    repo_url: str,
    ref: Optional[str],
    env_keys: List[str],
    commit_sha: Optional[str],
    preferred_repo_path: Optional[Path],
    enable_submodules: bool,
    enable_lfs: bool,
    force: bool,
    mode: str,
    log: Callable[[str], None],
    llm: Optional[LLMAdapter] = None,
    report_store: Optional[ReportStore] = None,
) -> AnalysisOutcome:
    if not repo_url:
        raise AnalysisFailure("ANALYZE_REPO_NOT_FOUND", "Missing repo_url")
    ref_value = normalize_ref(ref)

    repo_path, cleanup_repo, resolved_ref = _ensure_repo_path(
        case_id,
        repo_url,
        ref_value,
        preferred_repo_path,
        enable_submodules,
        enable_lfs,
        log,
    )
    if resolved_ref:
        log(f"[analyze] resolved_ref={resolved_ref}")
    try:
        resolved_commit = commit_sha or _resolve_commit_sha(repo_path)
        if not resolved_commit:
            fingerprint = _compute_repo_fingerprint(repo_path)
            resolved_commit = f"snapshot-{fingerprint[:12]}"
            log("[analyze] commit sha unavailable; using snapshot fingerprint")

        store = report_store or ReportStore()

        if not force:
            cached = store.load_report(repo_url, resolved_commit)
            if cached and _has_business_summary(cached):
                log("[analyze] cache hit")
                return AnalysisOutcome(report=cached, cache_hit=True, commit_sha=resolved_commit, signals_path=None)
            if cached:
                log("[analyze] cache hit (missing business summary)")
                cached_signals = store.load_signals(repo_url, resolved_commit)
                signals = cached_signals or extract_signals(repo_path, env_keys)
                if not cached_signals:
                    store.save_signals(repo_url, resolved_commit, signals)
                llm_adapter = llm or get_default_llm()
                business_summary = _build_business_summary(repo_url, signals, llm_adapter, log)
                cached["business_summary"] = business_summary
                store.save_report(repo_url, resolved_commit, cached)
                return AnalysisOutcome(
                    report=cached,
                    cache_hit=False,
                    commit_sha=resolved_commit,
                    signals_path=None,
                )

        signals = extract_signals(repo_path, env_keys)
        signals_path = str(store.save_signals(repo_url, resolved_commit, signals))

        llm_adapter = llm or get_default_llm()
        business_summary = _build_business_summary(repo_url, signals, llm_adapter, log)
        raw_response = llm_adapter.generate(signals)
        markdown, mermaids = _parse_llm_output(raw_response)
        if _should_translate_markdown(markdown):
            try:
                log("[analyze] markdown not in Chinese; translating")
                translated = llm_adapter.translate_markdown(markdown)
                if _contains_cjk(translated):
                    markdown = translated
            except Exception as exc:  # noqa: BLE001
                log(f"[analyze] markdown translation failed: {exc}")

        if not mermaids:
            log("[analyze] LLM returned no mermaid, using fallback")
            mermaids = [_default_mermaid(signals)]

        report_dir = store.report_dir_for(repo_url, resolved_commit)
        final_mermaids, validations, assets, _ = _validate_and_fix_mermaids(mermaids, llm_adapter, report_dir)

        sanitized_markdown = sanitize_text(markdown)
        sanitized_markdown = _append_mermaid_failures(sanitized_markdown, final_mermaids, validations)
        sanitized_mermaids = [sanitize_text(code) for code in final_mermaids]

        report_payload = _build_report_payload(
            case_id=case_id,
            repo_url=repo_url,
            commit_sha=resolved_commit,
            markdown=sanitized_markdown,
            mermaids=sanitized_mermaids,
            assets=assets,
            validations=validations,
            signals_path=os.path.relpath(signals_path, report_dir) if signals_path else None,
            business_summary=business_summary,
        )
        store.save_report(repo_url, resolved_commit, report_payload)

        return AnalysisOutcome(
            report=report_payload,
            cache_hit=False,
            commit_sha=resolved_commit,
            signals_path=signals_path,
        )
    finally:
        if cleanup_repo:
            shutil.rmtree(repo_path, ignore_errors=True)
