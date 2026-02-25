import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from analyze.signals import sanitize_text
from config import INGEST_GIT_DEPTH, INGEST_MAX_FILES
from git_ops import normalize_ref
from ingest.openclaw import OpenClawClient


@dataclass
class IngestOutcome:
    repo_path: Path
    commit_sha: str
    ingest_meta: Dict[str, Any]
    meta_path: Path
    cleanup_repo: bool


class IngestFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_commit_sha(repo_path: Path) -> Optional[str]:
    try:
        return (
            subprocess.check_output(["git", "-C", str(repo_path), "rev-parse", "HEAD"], text=True)
            .strip()
        )
    except Exception:
        return None


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


def _scan_repo_stats(repo_path: Path, max_files: int = 20000) -> Dict[str, Any]:
    total_files = 0
    total_bytes = 0
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "venv", ".venv"}]
        for name in files:
            path = Path(root) / name
            try:
                total_bytes += path.stat().st_size
                total_files += 1
            except Exception:
                continue
            if total_files >= max_files:
                return {"file_count": total_files, "byte_count": total_bytes, "truncated": True}
    return {"file_count": total_files, "byte_count": total_bytes, "truncated": False}


def _sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        raw = json.dumps(payload, ensure_ascii=False)
        sanitized = sanitize_text(raw)
        return json.loads(sanitized)
    except Exception:
        return {}


def run_ingest(
    case_id: str,
    repo_url: str,
    ref: Optional[str],
    preferred_repo_path: Optional[Path],
    enable_submodules: bool,
    enable_lfs: bool,
    log: Callable[[str], None],
    output_dir: Path,
    openclaw: Optional[OpenClawClient] = None,
) -> IngestOutcome:
    if not repo_url:
        raise IngestFailure("INGEST_REPO_NOT_FOUND", "Missing repo_url")
    ref_value = normalize_ref(ref)
    try:
        client = openclaw or OpenClawClient()
        if not client.available:
            raise IngestFailure("INGEST_FAILED", "OpenClaw unavailable")

        log("[ingest] openclaw github.fetch")
        payload = {
            "repo_url": repo_url,
            "ref": ref_value,
            "depth": INGEST_GIT_DEPTH,
            "include_submodules": enable_submodules,
            "include_lfs": enable_lfs,
            "max_files": INGEST_MAX_FILES,
        }
        result = client.run_skill("github.fetch", payload)
        response = result.payload or {}

        if isinstance(response, dict) and response.get("ok") is False:
            raise IngestFailure(
                str(response.get("error_code") or "INGEST_FAILED"),
                str(response.get("error_message") or "OpenClaw github.fetch failed"),
            )

        output = response.get("output") if isinstance(response, dict) else None
        if output is None:
            output = response if isinstance(response, dict) else {}

        repo_path_value = output.get("repo_path") if isinstance(output, dict) else None
        if preferred_repo_path and preferred_repo_path.exists():
            repo_path = preferred_repo_path
        elif repo_path_value:
            repo_path = Path(str(repo_path_value))
        else:
            raise IngestFailure("INGEST_FAILED", "OpenClaw missing repo_path")

        commit_sha = str(output.get("commit_sha") or "") if isinstance(output, dict) else ""
        if not commit_sha:
            commit_sha = _resolve_commit_sha(repo_path) or ""
        if not commit_sha:
            fingerprint = _compute_repo_fingerprint(repo_path)
            commit_sha = f"snapshot-{fingerprint[:12]}"
            log("[ingest] commit sha unavailable; using snapshot fingerprint")

        meta: Dict[str, Any] = {
            "generated_at": time.time(),
            "repo_url": repo_url,
            "ref": ref_value,
            "commit_sha": commit_sha,
            "repo_path": str(repo_path),
            "stats": _scan_repo_stats(repo_path),
            "openclaw": {
                "attempted": True,
                "ok": True,
                "endpoint": result.endpoint,
                "duration_ms": result.duration_ms,
            },
            "openclaw_payload": _sanitize_payload(response if isinstance(response, dict) else {}),
            "file_index": output.get("file_index") if isinstance(output, dict) else None,
            "readme_rendered": output.get("readme_rendered") if isinstance(output, dict) else None,
            "repo_meta": output.get("repo_meta") if isinstance(output, dict) else None,
            "repo_meta_available": output.get("repo_meta_available") if isinstance(output, dict) else None,
            "repo_meta_reason": output.get("repo_meta_reason") if isinstance(output, dict) else None,
        }

        output_dir.mkdir(parents=True, exist_ok=True)
        meta_path = output_dir / "ingest_meta.json"
        if isinstance(output, dict) and output.get("ingest_meta_path"):
            try:
                source = Path(str(output.get("ingest_meta_path")))
                if source.exists():
                    shutil.copyfile(source, meta_path)
                else:
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return IngestOutcome(
            repo_path=repo_path,
            commit_sha=commit_sha,
            ingest_meta=meta,
            meta_path=meta_path,
            cleanup_repo=False,
        )
    except IngestFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise IngestFailure("INGEST_FAILED", str(exc)) from exc
