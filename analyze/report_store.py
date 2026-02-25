import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from config import REPORT_ROOT

_SLUG_CLEAN_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def repo_slug(repo_url: str) -> str:
    raw = (repo_url or "").strip() or "repo"
    normalized = raw.replace("://", "-").replace("/", "-").replace("\\", "-")
    cleaned = _SLUG_CLEAN_PATTERN.sub("-", normalized).strip("-")
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:8]
    if not cleaned:
        cleaned = "repo"
    if len(cleaned) > 60:
        cleaned = cleaned[:60].rstrip("-")
    return f"{cleaned}-{digest}"


def repo_cache_key(repo_url: str, commit_sha: str) -> str:
    raw = (repo_url or "").strip()
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return f"{digest}:{commit_sha}"


def report_root() -> Path:
    root = Path(REPORT_ROOT)
    if not root.is_absolute():
        root = Path(os.getcwd()) / root
    return root


def report_dir(repo_url: str) -> Path:
    return report_root() / repo_slug(repo_url)


def report_path(repo_url: str, commit_sha: str) -> Path:
    return report_dir(repo_url) / f"{commit_sha}.json"


def signals_path(repo_url: str, commit_sha: str) -> Path:
    return report_dir(repo_url) / f"{commit_sha}.signals.json"


class ReportStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or report_root()

    def _report_path(self, repo_url: str, commit_sha: str) -> Path:
        base = self._root / repo_slug(repo_url)
        return base / f"{commit_sha}.json"

    def _signals_path(self, repo_url: str, commit_sha: str) -> Path:
        base = self._root / repo_slug(repo_url)
        return base / f"{commit_sha}.signals.json"

    def has_report(self, repo_url: str, commit_sha: str) -> bool:
        return self._report_path(repo_url, commit_sha).exists()

    def load_report(self, repo_url: str, commit_sha: str) -> Optional[Dict[str, Any]]:
        path = self._report_path(repo_url, commit_sha)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def load_signals(self, repo_url: str, commit_sha: str) -> Optional[Dict[str, Any]]:
        path = self._signals_path(repo_url, commit_sha)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_report(self, repo_url: str, commit_sha: str, payload: Dict[str, Any]) -> Path:
        path = self._report_path(repo_url, commit_sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def save_signals(self, repo_url: str, commit_sha: str, payload: Dict[str, Any]) -> Path:
        path = self._signals_path(repo_url, commit_sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def list_reports(self, repo_url: str) -> list[Path]:
        base = self._root / repo_slug(repo_url)
        if not base.exists():
            return []
        return sorted([path for path in base.glob("*.json") if path.is_file()])

    def report_dir_for(self, repo_url: str, commit_sha: str) -> Path:
        return self._report_path(repo_url, commit_sha).parent

    def report_path_for(self, repo_url: str, commit_sha: str) -> Path:
        return self._report_path(repo_url, commit_sha)
