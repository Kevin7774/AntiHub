import json
from pathlib import Path
from typing import Any, Dict, Optional

from analyze.report_store import report_dir
from config import VISUAL_TEMPLATE_VERSION


def _safe_version(version: str) -> str:
    value = (version or "v1").strip()
    return value.replace("/", "_").replace("\\", "_").replace(" ", "_")


def visuals_path(repo_url: str, commit_sha: str, template_version: str | None = None) -> Path:
    safe_version = _safe_version(template_version or VISUAL_TEMPLATE_VERSION)
    return report_dir(repo_url) / f"{commit_sha}.{safe_version}.visuals.json"


def visuals_dir(repo_url: str, commit_sha: str, template_version: str | None = None) -> Path:
    safe_commit = (commit_sha or "unknown").replace("/", "_").replace("\\", "_")
    safe_version = _safe_version(template_version or VISUAL_TEMPLATE_VERSION)
    return report_dir(repo_url) / f"{safe_commit}-{safe_version}"


def visual_cache_key(repo_url: str, commit_sha: str, template_version: str | None = None) -> str:
    safe_version = _safe_version(template_version or VISUAL_TEMPLATE_VERSION)
    raw = (repo_url or "").strip()
    return f"{raw}:{commit_sha}:{safe_version}"


class VisualStore:
    def __init__(self, template_version: str | None = None) -> None:
        self.template_version = template_version or VISUAL_TEMPLATE_VERSION

    def _visuals_path(self, repo_url: str, commit_sha: str) -> Path:
        return visuals_path(repo_url, commit_sha, self.template_version)

    def _visuals_dir(self, repo_url: str, commit_sha: str) -> Path:
        return visuals_dir(repo_url, commit_sha, self.template_version)

    def has_visuals(self, repo_url: str, commit_sha: str) -> bool:
        return self._visuals_path(repo_url, commit_sha).exists()

    def load_visuals(self, repo_url: str, commit_sha: str) -> Optional[Dict[str, Any]]:
        path = self._visuals_path(repo_url, commit_sha)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_visuals(self, repo_url: str, commit_sha: str, payload: Dict[str, Any]) -> Path:
        path = self._visuals_path(repo_url, commit_sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def visuals_dir_for(self, repo_url: str, commit_sha: str) -> Path:
        path = self._visuals_dir(repo_url, commit_sha)
        path.mkdir(parents=True, exist_ok=True)
        return path
