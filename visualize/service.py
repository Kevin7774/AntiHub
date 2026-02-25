import base64
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from analyze.product_story import build_product_story
from analyze.report_store import ReportStore, signals_path
from analyze.signals import extract_signals, sanitize_text
from config import (
    INGEST_ROOT,
    MERMAID_INSTALL_TIMEOUT_SECONDS,
    MERMAID_VALIDATE_TIMEOUT_SECONDS,
    VISUAL_REMOTION_PROJECT,
    VISUAL_TEMPLATE_VERSION,
    VISUAL_VIDEO_ENABLED,
)
from evidence import validate_evidence
from ingest.service import IngestFailure, run_ingest
from visualize.image_client import ImageClient, ImageClientError
from visualize.node_tools import find_node_bin
from visualize.pack import (
    build_knowledge_graph,
    build_repo_graph,
    build_repo_index,
    build_storyboard,
    select_spotlights,
)
from visualize.remotion_renderer import RemotionRenderError, render_visual_video
from visualize.store import VisualStore

DEFAULT_KINDS = (
    "repo_index",
    "repo_graph",
    "knowledge_graph",
    "spotlights",
    "storyboard",
    "product_story",
    "architecture_poster",
    "pipeline_sequence",
)

DEFAULT_SEQUENCE = """sequenceDiagram
  participant UI
  participant API
  participant Redis
  participant Worker
  participant Docker
  participant WS as WS Logs
  participant Store as Report Store

  UI->>API: POST /cases/{id}/analyze
  API->>Redis: enqueue analyze task
  Redis->>Worker: run analyze job
  Worker->>Docker: build/run repo
  Worker->>WS: stream logs
  Worker->>Store: save report/visuals
  UI->>API: GET /cases/{id}/report
  API-->>UI: return report/visuals
"""


@dataclass
class VisualOutcome:
    payload: Dict[str, Any]
    cache_hit: bool
    commit_sha: str


class VisualFailure(RuntimeError):
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
    return stripped.strip()


def _detect_mermaid_type(code: str) -> str:
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0].lower()
    if first.startswith("sequencediagram"):
        return "sequence"
    if first.startswith("flowchart") or first.startswith("graph"):
        return "flowchart"
    return first.split()[0]


def _select_mermaid(mermaids: List[str], preferred: Tuple[str, ...]) -> Optional[str]:
    for code in mermaids:
        mermaid_type = _detect_mermaid_type(code)
        if mermaid_type in preferred:
            return code
    return mermaids[0] if mermaids else None


def _default_mermaid(signals: Dict[str, Any]) -> str:
    repo_hint = "App"
    readme = signals.get("readme") or {}
    headings = readme.get("headings") or []
    if headings:
        repo_hint = str(headings[0]).strip() or repo_hint
    repo_id = _safe_mermaid_id(repo_hint)
    return f"""flowchart LR
  User[User] --> {repo_id}[{repo_hint}]
  {repo_id} --> Service[Services]
  Service --> Dependencies[Dependencies]
"""


def _safe_mermaid_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", (value or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return "App"
    if cleaned[0].isdigit():
        return f"R_{cleaned}"
    return cleaned


MERMAID_WORKSPACE = Path(__file__).resolve().parent / ".mermaid-cli"
_MERMAID_COMMAND: Optional[List[str]] = None
_MERMAID_METHOD: str = "mmdc"


def _wait_for_mermaid_cli(project_dir: Path, timeout_seconds: int) -> Optional[List[str]]:
    local_mmdc = project_dir / "node_modules" / ".bin" / "mmdc"
    deadline = time.time() + max(1, timeout_seconds)
    while time.time() < deadline:
        if local_mmdc.exists():
            return [str(local_mmdc)]
        time.sleep(1)
    return None


def _resolve_mermaid_command() -> Tuple[Optional[List[str]], str]:
    global _MERMAID_COMMAND, _MERMAID_METHOD
    if _MERMAID_COMMAND:
        return _MERMAID_COMMAND, _MERMAID_METHOD
    mmdc = shutil.which("mmdc")
    if mmdc:
        _MERMAID_COMMAND = [mmdc]
        _MERMAID_METHOD = "mmdc"
        return _MERMAID_COMMAND, _MERMAID_METHOD
    workspace_dir = MERMAID_WORKSPACE
    workspace_dir.mkdir(parents=True, exist_ok=True)
    package_json = workspace_dir / "package.json"
    if not package_json.exists():
        package_json.write_text(
            json.dumps({"name": "antihub-mermaid", "private": True, "version": "0.0.0"}),
            encoding="utf-8",
        )
    local_mmdc = workspace_dir / "node_modules" / ".bin" / "mmdc"
    if local_mmdc.exists():
        _MERMAID_COMMAND = [str(local_mmdc)]
        _MERMAID_METHOD = "mmdc"
        return _MERMAID_COMMAND, _MERMAID_METHOD
    npm = find_node_bin("npm")
    if npm:
        lock_path = workspace_dir / ".mermaid-install.lock"
        if lock_path.exists():
            waited = _wait_for_mermaid_cli(workspace_dir, MERMAID_INSTALL_TIMEOUT_SECONDS)
            if waited:
                _MERMAID_COMMAND = waited
                _MERMAID_METHOD = "mmdc"
                return _MERMAID_COMMAND, _MERMAID_METHOD
        else:
            try:
                lock_path.write_text(str(time.time()), encoding="utf-8")
            except Exception:
                lock_path = None
            try:
                subprocess.run(
                    [npm, "install", "--no-audit", "--no-fund", "@mermaid-js/mermaid-cli"],
                    cwd=str(workspace_dir),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=MERMAID_INSTALL_TIMEOUT_SECONDS,
                )
            finally:
                if lock_path and lock_path.exists():
                    try:
                        lock_path.unlink()
                    except Exception:
                        pass
            if local_mmdc.exists():
                _MERMAID_COMMAND = [str(local_mmdc)]
                _MERMAID_METHOD = "mmdc"
                return _MERMAID_COMMAND, _MERMAID_METHOD
    if local_mmdc.exists():
        _MERMAID_COMMAND = [str(local_mmdc)]
        _MERMAID_METHOD = "mmdc"
        return _MERMAID_COMMAND, _MERMAID_METHOD
    npx = find_node_bin("npx")
    if npx:
        _MERMAID_COMMAND = [npx, "--yes", "@mermaid-js/mermaid-cli"]
        _MERMAID_METHOD = "npx"
        return _MERMAID_COMMAND, _MERMAID_METHOD
    return None, "mmdc"


def _install_headless_chrome() -> bool:
    npx = find_node_bin("npx")
    if not npx:
        return False
    try:
        result = subprocess.run(
            [npx, "--yes", "puppeteer", "browsers", "install", "chrome-headless-shell"],
            cwd=str(MERMAID_WORKSPACE),
            check=False,
            capture_output=True,
            text=True,
            timeout=MERMAID_INSTALL_TIMEOUT_SECONDS,
        )
    except Exception:
        return False
    return result.returncode == 0


def _find_existing_chrome() -> Optional[str]:
    remotion_root = Path(VISUAL_REMOTION_PROJECT) / "node_modules" / ".remotion"
    if remotion_root.exists():
        for candidate in remotion_root.rglob("chrome-headless-shell"):
            if candidate.is_file():
                return str(candidate)
    return None


def _render_mermaid_png(code: str, output_path: Path) -> Tuple[bool, str, str]:
    cleaned = _strip_fences(code)
    if not cleaned:
        return False, "mermaid code is empty", "mmdc"
    command, method = _resolve_mermaid_command()
    if not command:
        return False, "mmdc not available", method
    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_path = output_path.with_suffix(".mmd")
    input_path.write_text(cleaned, encoding="utf-8")
    puppeteer_config = output_path.with_suffix(".puppeteer.json")
    puppeteer_config.write_text(
        json.dumps(
            {
                "args": ["--no-sandbox", "--disable-setuid-sandbox"],
            }
        ),
        encoding="utf-8",
    )
    env = None
    chrome_path = _find_existing_chrome()
    if chrome_path:
        env = dict(os.environ)
        env["PUPPETEER_EXECUTABLE_PATH"] = chrome_path
        env["PUPPETEER_SKIP_DOWNLOAD"] = "1"
    try:
        result = subprocess.run(
            command
            + [
                "-i",
                str(input_path),
                "-o",
                str(output_path),
                "--quiet",
                "-t",
                "neutral",
                "--puppeteerConfigFile",
                str(puppeteer_config),
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=MERMAID_VALIDATE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return False, f"mmdc failed: {exc}", method
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "mmdc render failed").strip()
        if "Could not find Chrome" in error or "chrome-headless-shell" in error:
            if _install_headless_chrome():
                retry = subprocess.run(
                    command
                    + [
                        "-i",
                        str(input_path),
                        "-o",
                        str(output_path),
                        "--quiet",
                        "-t",
                        "neutral",
                        "--puppeteerConfigFile",
                        str(puppeteer_config),
                    ],
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=MERMAID_VALIDATE_TIMEOUT_SECONDS,
                )
                if retry.returncode == 0 and output_path.exists():
                    return True, "ok", method
                error = (retry.stderr or retry.stdout or error).strip()
        return False, error, method
    if not output_path.exists():
        return False, "mmdc did not produce output", method
    return True, "ok", method


def _build_poster_prompt(title: str, description: str) -> str:
    return (
        "You are a product designer. Refine the reference architecture diagram into a clean, modern poster. "
        "Use a consistent color palette, card-style components, clear hierarchy, and subtle shadows. "
        "Preserve all nodes/edges and labels; do not add new components. "
        f"Add a concise title: {title}. "
        f"Add a short subtitle describing the system: {description}."
    )


def _load_signals(repo_url: str, commit_sha: str, report_dir: Path, report: Dict[str, Any]) -> Dict[str, Any]:
    rel_path = report.get("signals_path")
    if rel_path:
        path = report_dir / rel_path
    else:
        path = signals_path(repo_url, commit_sha)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _has_line_range(line_range: Any) -> bool:
    if not isinstance(line_range, dict):
        return False
    return bool(line_range.get("start") and line_range.get("end"))


def _validate_spotlights_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    items = payload.get("items")
    if not isinstance(items, list):
        return False
    if not items:
        return True
    for item in items:
        if not isinstance(item, dict):
            return False
        if not _has_line_range(item.get("line_range")):
            return False
        if not validate_evidence(item.get("evidence") or {}):
            return False
    return True


def _validate_storyboard_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if "evidence_catalog" not in payload:
        return False
    catalog = payload.get("evidence_catalog") or []
    evidence_by_id = {
        item.get("id"): item for item in catalog if isinstance(item, dict) and validate_evidence(item)
    }
    scenes = payload.get("scenes") or []
    if not scenes:
        return True
    if not isinstance(scenes, list):
        return False
    for scene in scenes:
        if not isinstance(scene, dict):
            return False
        shots = scene.get("shots")
        if not isinstance(shots, list):
            return False
        for shot in shots:
            if not isinstance(shot, dict):
                return False
            evidence_id = shot.get("evidence_id")
            if not evidence_id or evidence_id not in evidence_by_id:
                return False
    return True


def _validate_product_story_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        return False
    return "evidence_catalog" in meta


def _validate_knowledge_graph_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return False
    node_ids = {
        item.get("id")
        for item in nodes
        if isinstance(item, dict) and item.get("id")
    }
    if not node_ids:
        return False
    for edge in edges:
        if not isinstance(edge, dict):
            return False
        source = edge.get("source")
        target = edge.get("target")
        if not source or not target:
            return False
        if source not in node_ids or target not in node_ids:
            return False
    return True


def run_visualize(
    case_id: str,
    repo_url: str,
    commit_sha: Optional[str],
    force: bool,
    kinds: Optional[List[str]],
    log: Callable[[str], None],
    report_store: Optional[ReportStore] = None,
    visual_store: Optional[VisualStore] = None,
    repo_dir: Optional[Path] = None,
    ref: Optional[str] = None,
    env_keys: Optional[List[str]] = None,
    enable_submodules: bool = False,
    enable_lfs: bool = False,
    render_webm: Optional[bool] = None,
) -> VisualOutcome:
    if not repo_url:
        raise VisualFailure("VISUALIZE_REPORT_NOT_READY", "Missing repo_url")

    if repo_dir is not None and not isinstance(repo_dir, Path):
        repo_dir = Path(repo_dir)

    store = visual_store or VisualStore()
    template_version = getattr(store, "template_version", None) or VISUAL_TEMPLATE_VERSION
    requested = [kind for kind in (kinds or list(DEFAULT_KINDS))]
    requested_set = set(requested)
    if "video" in requested_set and not VISUAL_VIDEO_ENABLED:
        requested_set.discard("video")
        if not requested_set:
            # Keep UX stable: degrade explicit video request to regular visual pack.
            requested_set.update(DEFAULT_KINDS)
        log("[visualize] video rendering disabled; fallback to image/text walkthrough")
    if "video" in requested_set:
        requested_set.update({"repo_index", "repo_graph", "spotlights", "storyboard"})
    if "repo_graph" in requested_set:
        requested_set.add("repo_index")
    if "knowledge_graph" in requested_set:
        requested_set.update({"repo_index", "repo_graph", "spotlights"})
    if "storyboard" in requested_set:
        requested_set.update({"repo_index", "repo_graph", "spotlights"})
    requested = list(requested_set)

    ingest_workspace = Path(INGEST_ROOT) / case_id
    try:
        ingest_outcome = run_ingest(
            case_id=case_id,
            repo_url=repo_url,
            ref=ref,
            preferred_repo_path=repo_dir,
            enable_submodules=enable_submodules,
            enable_lfs=enable_lfs,
            log=log,
            output_dir=ingest_workspace,
        )
    except IngestFailure as exc:
        raise VisualFailure(exc.code, exc.message) from exc

    resolved_commit = ingest_outcome.commit_sha
    if commit_sha and commit_sha != resolved_commit:
        log(f"[visualize] commit sha updated {commit_sha} -> {resolved_commit}")
    commit_sha = resolved_commit

    try:
        visuals_dir = store.visuals_dir_for(repo_url, commit_sha)
        ingest_meta_path = visuals_dir / "ingest_meta.json"
        if ingest_outcome.meta_path.exists() and ingest_outcome.meta_path != ingest_meta_path:
            shutil.copyfile(ingest_outcome.meta_path, ingest_meta_path)

        cached = store.load_visuals(repo_url, commit_sha)
        cached_assets: Dict[str, Dict[str, Any]] = {}
        if cached and isinstance(cached.get("assets"), list):
            for item in cached.get("assets") or []:
                if isinstance(item, dict) and item.get("kind"):
                    cached_assets[str(item.get("kind"))] = item

        def _load_cached_json(asset: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            files = asset.get("files") or []
            if not files:
                return None
            path = visuals_dir / files[0]
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None

        def _invalidate_cached(kind: str, reason: str) -> None:
            if kind in cached_assets:
                cached_assets.pop(kind, None)
                log(f"[visualize] invalidate cached {kind}: {reason}")

        if cached_assets:
            spotlights_asset = cached_assets.get("spotlights")
            if spotlights_asset:
                payload = _load_cached_json(spotlights_asset)
                if not payload or not _validate_spotlights_payload(payload):
                    _invalidate_cached("spotlights", "missing evidence")

            storyboard_asset = cached_assets.get("storyboard")
            if storyboard_asset:
                payload = _load_cached_json(storyboard_asset)
                if not payload or not _validate_storyboard_payload(payload):
                    _invalidate_cached("storyboard", "missing evidence")

            product_story_asset = cached_assets.get("product_story")
            if product_story_asset:
                payload = _load_cached_json(product_story_asset)
                if not payload or not _validate_product_story_payload(payload):
                    _invalidate_cached("product_story", "missing evidence")

            knowledge_graph_asset = cached_assets.get("knowledge_graph")
            if knowledge_graph_asset:
                payload = _load_cached_json(knowledge_graph_asset)
                if not payload or not _validate_knowledge_graph_payload(payload):
                    _invalidate_cached("knowledge_graph", "invalid graph")

        def asset_ready(kind: str) -> bool:
            asset = cached_assets.get(kind)
            return bool(asset and str(asset.get("status") or "").upper() == "SUCCESS")

        if cached and not force and all(asset_ready(kind) for kind in requested) and ingest_meta_path.exists():
            cached["cached"] = True
            cached.setdefault("template_version", template_version)
            log("[visualize] cache hit")
            return VisualOutcome(payload=cached, cache_hit=True, commit_sha=commit_sha)

        report_store = report_store or ReportStore()
        report = report_store.load_report(repo_url, commit_sha)
        report_dir = report_store.report_dir_for(repo_url, commit_sha)
        signals = _load_signals(repo_url, commit_sha, report_dir, report or {})
        if not signals:
            signals = extract_signals(ingest_outcome.repo_path, env_keys or [])

        title = str(signals.get("repo_name") or signals.get("repo") or "Architecture").strip()
        if not title:
            title = "Architecture"
        description = "Automated repo explain & deployment pipeline"
        if signals.get("summary"):
            description = str(signals.get("summary"))[:120]

        mermaids: List[str] = []
        if report:
            mermaids = list(report.get("mermaids") or [])
            mermaids = [sanitize_text(_strip_fences(code)) for code in mermaids if code]

        created_at = time.time()
        assets_by_kind: Dict[str, Dict[str, Any]] = {}
        if cached and not force:
            for kind, asset in cached_assets.items():
                if kind in requested_set or kind == "ingest_meta":
                    assets_by_kind[kind] = asset

        def record_asset(
            kind: str,
            status: str,
            files: List[str],
            meta: Dict[str, Any],
            error_code: Optional[str] = None,
            error_message: Optional[str] = None,
        ) -> None:
            assets_by_kind[kind] = {
                "kind": kind,
                "status": status,
                "files": files,
                "meta": meta,
                "created_at": created_at,
                "error_code": error_code,
                "error_message": error_message,
            }

        def ensure_json_asset(kind: str, data: Dict[str, Any], filename: str) -> None:
            target = visuals_dir / filename
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            record_asset(kind=kind, status="SUCCESS", files=[target.name], meta={})

        record_asset(
            kind="ingest_meta",
            status="SUCCESS",
            files=[ingest_meta_path.name],
            meta={"template_version": template_version},
        )

        if "repo_index" in requested and (force or not asset_ready("repo_index")):
            try:
                log("[visualize] build repo_index")
                repo_index = build_repo_index(
                    ingest_outcome.repo_path,
                    repo_url=repo_url,
                    env_keys=env_keys,
                    commit_sha=commit_sha,
                    template_version=template_version,
                    ingest_meta=ingest_outcome.ingest_meta,
                )
                ensure_json_asset("repo_index", repo_index, "repo_index.json")
            except Exception as exc:  # noqa: BLE001
                record_asset(
                    kind="repo_index",
                    status="FAILED",
                    files=[],
                    meta={},
                    error_code="VISUALIZE_INDEX_FAILED",
                    error_message=str(exc),
                )

        repo_index_payload = assets_by_kind.get("repo_index")
        repo_index_data: Optional[Dict[str, Any]] = None
        if repo_index_payload and repo_index_payload.get("files"):
            try:
                repo_index_data = json.loads(
                    (visuals_dir / repo_index_payload["files"][0]).read_text(encoding="utf-8")
                )
            except Exception:
                repo_index_data = None

        if "repo_graph" in requested and (force or not asset_ready("repo_graph")) and repo_index_data:
            try:
                log("[visualize] build repo_graph")
                repo_graph = build_repo_graph(repo_index_data)
                ensure_json_asset("repo_graph", repo_graph, "repo_graph.json")
            except Exception as exc:  # noqa: BLE001
                record_asset(
                    kind="repo_graph",
                    status="FAILED",
                    files=[],
                    meta={},
                    error_code="VISUALIZE_GRAPH_FAILED",
                    error_message=str(exc),
                )
        elif "repo_graph" in requested and not repo_index_data:
            record_asset(
                kind="repo_graph",
                status="FAILED",
                files=[],
                meta={},
                error_code="VISUALIZE_GRAPH_FAILED",
                error_message="repo_index missing",
            )

        if "spotlights" in requested and (force or not asset_ready("spotlights")):
            try:
                log("[visualize] build spotlights")
                spotlights = select_spotlights(ingest_outcome.repo_path)
                items = spotlights.get("items") if isinstance(spotlights, dict) else None
                if isinstance(items, list):
                    filtered = [
                        item
                        for item in items
                        if isinstance(item, dict)
                        and _has_line_range(item.get("line_range"))
                        and validate_evidence(item.get("evidence") or {})
                    ]
                    if len(filtered) != len(items):
                        log(
                            f"[visualize] filter spotlights: {len(items)} -> {len(filtered)} (missing evidence)"
                        )
                    spotlights["items"] = filtered
                ensure_json_asset("spotlights", spotlights, "spotlights.json")
            except Exception as exc:  # noqa: BLE001
                record_asset(
                    kind="spotlights",
                    status="FAILED",
                    files=[],
                    meta={},
                    error_code="VISUALIZE_SPOTLIGHT_FAILED",
                    error_message=str(exc),
                )

        repo_graph_payload = assets_by_kind.get("repo_graph")
        repo_graph_data: Optional[Dict[str, Any]] = None
        if repo_graph_payload and repo_graph_payload.get("files"):
            try:
                repo_graph_data = json.loads(
                    (visuals_dir / repo_graph_payload["files"][0]).read_text(encoding="utf-8")
                )
            except Exception:
                repo_graph_data = None

        spotlights_payload = assets_by_kind.get("spotlights")
        spotlights_data: Optional[Dict[str, Any]] = None
        if spotlights_payload and spotlights_payload.get("files"):
            try:
                spotlights_data = json.loads(
                    (visuals_dir / spotlights_payload["files"][0]).read_text(encoding="utf-8")
                )
            except Exception:
                spotlights_data = None

        if "product_story" in requested and (force or not asset_ready("product_story")):
            try:
                log("[visualize] build product_story")
                base_repo_index = repo_index_data or {
                    "repo_name": ingest_outcome.repo_path.name,
                    "repo_meta": (ingest_outcome.ingest_meta or {}).get("repo_meta") or {},
                    "tree": {},
                    "languages": [],
                    "dependencies": {},
                    "entrypoints": [],
                }
                readme_excerpt = ""
                if repo_index_data:
                    readme_excerpt = (
                        (repo_index_data.get("readme_summary") or {}).get("text") or ""
                    )
                if not readme_excerpt:
                    readme_excerpt = (signals.get("readme") or {}).get("excerpt") or ""
                outcome = build_product_story(base_repo_index, readme_excerpt, spotlights_data, log)
                target = visuals_dir / "product_story.json"
                target.write_text(
                    json.dumps(outcome.story, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                record_asset(
                    kind="product_story",
                    status="SUCCESS",
                    files=[target.name],
                    meta={"source": outcome.source, "error": outcome.error},
                )
            except Exception as exc:  # noqa: BLE001
                record_asset(
                    kind="product_story",
                    status="FAILED",
                    files=[],
                    meta={},
                    error_code="VISUALIZE_PRODUCT_STORY_FAILED",
                    error_message=str(exc),
                )

        if "knowledge_graph" in requested and (force or not asset_ready("knowledge_graph")):
            if repo_index_data and repo_graph_data:
                try:
                    log("[visualize] build knowledge_graph")
                    knowledge_graph = build_knowledge_graph(
                        repo_index_data,
                        repo_graph_data,
                        spotlights_data or {"items": []},
                    )
                    ensure_json_asset("knowledge_graph", knowledge_graph, "knowledge_graph.json")
                except Exception as exc:  # noqa: BLE001
                    record_asset(
                        kind="knowledge_graph",
                        status="FAILED",
                        files=[],
                        meta={},
                        error_code="VISUALIZE_GRAPH_FAILED",
                        error_message=str(exc),
                    )
            else:
                record_asset(
                    kind="knowledge_graph",
                    status="FAILED",
                    files=[],
                    meta={},
                    error_code="VISUALIZE_GRAPH_FAILED",
                    error_message="knowledge graph dependencies missing",
                )

        if (
            "storyboard" in requested
            and (force or not asset_ready("storyboard"))
            and repo_index_data
            and repo_graph_data
            and spotlights_data
        ):
            try:
                log("[visualize] build storyboard")
                storyboard = build_storyboard(repo_index_data, repo_graph_data, spotlights_data, template_version)
                ensure_json_asset("storyboard", storyboard, "storyboard.json")
            except Exception as exc:  # noqa: BLE001
                record_asset(
                    kind="storyboard",
                    status="FAILED",
                    files=[],
                    meta={},
                    error_code="VISUALIZE_STORYBOARD_FAILED",
                    error_message=str(exc),
                )
        elif "storyboard" in requested and not (repo_index_data and repo_graph_data and spotlights_data):
            record_asset(
                kind="storyboard",
                status="FAILED",
                files=[],
                meta={},
                error_code="VISUALIZE_STORYBOARD_FAILED",
                error_message="dependencies missing",
            )

        if "architecture_poster" in requested and (force or not asset_ready("architecture_poster")):
            arch_mermaid = _select_mermaid(mermaids, ("flowchart", "graph")) or _default_mermaid(signals)
            arch_reference = visuals_dir / "architecture_reference.png"
            ok, error_message, method = _render_mermaid_png(arch_mermaid, arch_reference)
            validation_item = {
                "mermaid": {
                    "ok": ok,
                    "method": method,
                    "error": None if ok else error_message,
                }
            }
            if not ok:
                record_asset(
                    kind="architecture_poster",
                    status="FAILED",
                    files=[],
                    meta={"validation": validation_item},
                    error_code="VISUALIZE_MERMAID_RENDER_FAILED",
                    error_message=error_message,
                )
            else:
                prompt = _build_poster_prompt(title, description)
                prompt = sanitize_text(prompt)
                seed = int(created_at) % 100000
                client = ImageClient()
                poster_path = visuals_dir / "architecture_poster.png"
                if not client.model:
                    shutil.copyfile(arch_reference, poster_path)
                    validation_item["minimax"] = {
                        "ok": False,
                        "provider": client.provider,
                        "endpoint": client.endpoint,
                        "model": client.model,
                        "seed": seed,
                        "error": "VISUAL_IMAGE_MODEL is missing",
                        "fallback": "reference",
                    }
                    record_asset(
                        kind="architecture_poster",
                        status="SUCCESS",
                        files=[poster_path.name],
                        meta={
                            "prompt": prompt,
                            "reference_file": arch_reference.name,
                            "validation": validation_item,
                            "fallback": "reference",
                        },
                    )
                else:
                    try:
                        image_b64 = base64.b64encode(arch_reference.read_bytes()).decode("utf-8")
                        log(
                            "[visualize] minimax request="
                            + json.dumps(
                                {
                                "endpoint": client.endpoint or client.provider,
                                "model": client.model,
                                "seed": seed,
                                "prompt_len": len(prompt),
                            }
                        )
                    )
                        result = client.generate(prompt=prompt, image_b64=image_b64, seed=seed)
                        poster_path.write_bytes(result.image_bytes)
                        log(
                            "[visualize] minimax response="
                            + json.dumps(
                                {
                                    "duration_ms": result.duration_ms,
                                    "keys": list(result.raw.keys()),
                                }
                            )
                        )
                        validation_item["minimax"] = {
                            "ok": True,
                            "provider": client.provider,
                            "endpoint": result.endpoint,
                            "model": result.model,
                            "seed": seed,
                            "duration_ms": result.duration_ms,
                        }
                        record_asset(
                            kind="architecture_poster",
                            status="SUCCESS",
                            files=[poster_path.name],
                            meta={
                                "prompt": prompt,
                                "reference_file": arch_reference.name,
                                "validation": validation_item,
                            },
                        )
                    except ImageClientError as exc:
                        log(f"[visualize] minimax error: {exc}")
                        shutil.copyfile(arch_reference, poster_path)
                        validation_item["minimax"] = {
                            "ok": False,
                            "provider": client.provider,
                            "endpoint": client.endpoint,
                            "model": client.model,
                            "seed": seed,
                            "error": str(exc),
                            "fallback": "reference",
                        }
                        record_asset(
                            kind="architecture_poster",
                            status="SUCCESS",
                            files=[poster_path.name],
                            meta={
                                "prompt": prompt,
                                "reference_file": arch_reference.name,
                                "validation": validation_item,
                                "fallback": "reference",
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        log(f"[visualize] minimax unexpected error: {exc}")
                        shutil.copyfile(arch_reference, poster_path)
                        validation_item["minimax"] = {
                            "ok": False,
                            "provider": client.provider,
                            "endpoint": client.endpoint,
                            "model": client.model,
                            "seed": seed,
                            "error": str(exc),
                            "fallback": "reference",
                        }
                        record_asset(
                            kind="architecture_poster",
                            status="SUCCESS",
                            files=[poster_path.name],
                            meta={
                                "prompt": prompt,
                                "reference_file": arch_reference.name,
                                "validation": validation_item,
                                "fallback": "reference",
                            },
                        )
                validation_file = visuals_dir / "architecture_poster.validation.json"
                try:
                    validation_payload = {
                        "kind": "architecture_poster",
                        "prompt": prompt,
                        "model": client.model,
                        "seed": seed,
                        "created_at": created_at,
                        "validation": validation_item,
                    }
                    validation_file.write_text(
                        json.dumps(validation_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        if "pipeline_sequence" in requested and (force or not asset_ready("pipeline_sequence")):
            seq_mermaid = _select_mermaid(mermaids, ("sequence",)) or DEFAULT_SEQUENCE
            seq_path = visuals_dir / "pipeline_sequence.png"
            ok, error_message, method = _render_mermaid_png(seq_mermaid, seq_path)
            validation_item = {
                "mermaid": {
                    "ok": ok,
                    "method": method,
                    "error": None if ok else error_message,
                }
            }
            if not ok:
                record_asset(
                    kind="pipeline_sequence",
                    status="FAILED",
                    files=[],
                    meta={"validation": validation_item},
                    error_code="VISUALIZE_MERMAID_RENDER_FAILED",
                    error_message=error_message,
                )
            else:
                record_asset(
                    kind="pipeline_sequence",
                    status="SUCCESS",
                    files=[seq_path.name],
                    meta={"validation": validation_item},
                )
                validation_file = visuals_dir / "pipeline_sequence.validation.json"
                try:
                    validation_payload = {
                        "kind": "pipeline_sequence",
                        "created_at": created_at,
                        "validation": validation_item,
                    }
                    validation_file.write_text(
                        json.dumps(validation_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        if "video" in requested and (force or not asset_ready("video")):
            if not (repo_index_data and repo_graph_data and spotlights_data):
                record_asset(
                    kind="video",
                    status="FAILED",
                    files=[],
                    meta={},
                    error_code="VISUALIZE_RENDER_FAILED",
                    error_message="video dependencies missing",
                )
            else:
                try:
                    log("[visualize] render video")
                    props = {
                        "repo_name": title,
                        "commit_sha": commit_sha,
                        "template_version": template_version,
                        "repo_index": repo_index_data,
                        "repo_graph": repo_graph_data,
                        "spotlights": spotlights_data,
                        "storyboard": None,
                    }
                    if assets_by_kind.get("storyboard", {}).get("files"):
                        storyboard_path = visuals_dir / assets_by_kind["storyboard"]["files"][0]
                        props["storyboard"] = json.loads(storyboard_path.read_text(encoding="utf-8"))
                    assets = {}
                    poster_path = visuals_dir / "architecture_poster.png"
                    if poster_path.exists():
                        assets["poster"] = poster_path
                    result = render_visual_video(
                        visuals_dir,
                        props=props,
                        assets=assets,
                        render_webm=render_webm,
                    )
                    record_asset(
                        kind="video",
                        status="SUCCESS",
                        files=result.files,
                        meta={"duration_ms": result.duration_ms, "command": result.command},
                    )
                except RemotionRenderError as exc:
                    record_asset(
                        kind="video",
                        status="FAILED",
                        files=[],
                        meta={},
                        error_code="VISUALIZE_RENDER_FAILED",
                        error_message=str(exc),
                    )
                except Exception as exc:  # noqa: BLE001
                    record_asset(
                        kind="video",
                        status="FAILED",
                        files=[],
                        meta={},
                        error_code="VISUALIZE_RENDER_FAILED",
                        error_message=str(exc),
                    )

        assets = list(assets_by_kind.values())
        status = "SUCCESS"
        if any(asset["status"] != "SUCCESS" for asset in assets):
            if any(asset["status"] == "SUCCESS" for asset in assets):
                status = "PARTIAL"
            else:
                status = "FAILED"

        payload = {
            "case_id": case_id,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "created_at": created_at,
            "status": status,
            "assets": assets,
            "cached": False,
            "template_version": template_version,
        }

        store.save_visuals(repo_url, commit_sha, payload)
        return VisualOutcome(payload=payload, cache_hit=False, commit_sha=commit_sha)
    finally:
        if ingest_outcome.cleanup_repo:
            shutil.rmtree(ingest_outcome.repo_path, ignore_errors=True)
