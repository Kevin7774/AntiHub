import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BACKUP_DOCKERFILE_SUFFIXES = {
    "orig",
    "original",
    "bak",
    "backup",
    "old",
    "save",
    "disabled",
}


class DockerfileAmbiguousError(RuntimeError):
    def __init__(self, candidates: List[str], meta: Optional[Dict[str, Any]] = None) -> None:
        message = (
            "Multiple Dockerfile candidates found: "
            f"{', '.join(candidates)}. Specify dockerfile_path/context_path to select one."
        )
        super().__init__(message)
        self.candidates = candidates
        self.meta: Dict[str, Any] = meta or {}


class DockerfileNotFoundError(FileNotFoundError):
    def __init__(self, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.meta: Dict[str, Any] = meta or {}


def resolve_dockerfile(
    repo_root: Path,
    dockerfile_path: Optional[str],
    context_path: Optional[str],
    search_depth: int = 2,
) -> Tuple[Path, Path, List[str], bool, Dict[str, Any]]:
    repo_root = repo_root.resolve()
    candidates: List[str] = []
    used_auto = False
    if dockerfile_path:
        candidate = (repo_root / dockerfile_path).resolve()
        if candidate.exists():
            try:
                candidate.relative_to(repo_root.resolve())
            except ValueError as exc:
                raise FileNotFoundError("dockerfile_path must be under repository root") from exc
            ctx = _resolve_context_path(repo_root, context_path, candidate.parent)
            meta = {
                "scanned_candidates": [],
                "primary_candidates": [],
                "backup_candidates": [],
                "ignored_backups": [],
                "non_unique_primary": False,
                "selected_dockerfile": str(candidate.relative_to(repo_root)),
                "selected_backup": _is_backup_dockerfile(candidate.name),
                "selection_reason": "explicit_path",
            }
            return candidate, ctx, candidates, used_auto, meta
        raise FileNotFoundError(f"Dockerfile not found at {dockerfile_path}")

    used_auto = True
    search_root = _resolve_search_root(repo_root, context_path)
    candidates = _find_dockerfiles(search_root, repo_root, search_depth)
    if not candidates:
        meta = {
            "scanned_candidates": [],
            "primary_candidates": [],
            "backup_candidates": [],
            "ignored_backups": [],
            "non_unique_primary": False,
            "selected_dockerfile": None,
            "selected_backup": False,
            "selection_reason": "not_found",
        }
        raise DockerfileNotFoundError("Dockerfile not found within search depth", meta)

    primary_candidates = [
        candidate for candidate in candidates if not _is_backup_dockerfile(Path(candidate).name)
    ]
    backup_candidates = [
        candidate for candidate in candidates if _is_backup_dockerfile(Path(candidate).name)
    ]
    non_unique_primary = len(primary_candidates) > 1
    ignored_backups: List[str] = []
    selected_backup = False
    considered_candidates: List[str] = []
    if primary_candidates:
        considered_candidates = primary_candidates
        ignored_backups = backup_candidates
    else:
        considered_candidates = backup_candidates
        selected_backup = True

    # Rule: if the context root Dockerfile exists, prefer it even when multiple candidates exist.
    root_candidate = str((search_root / "Dockerfile").relative_to(repo_root))
    if root_candidate in considered_candidates:
        selected_rel = root_candidate
        selection_reason = "root_dockerfile"
    elif len(considered_candidates) == 1:
        selected_rel = considered_candidates[0]
        selection_reason = "single_candidate"
    else:
        meta = {
            "scanned_candidates": candidates,
            "primary_candidates": primary_candidates,
            "backup_candidates": backup_candidates,
            "ignored_backups": ignored_backups,
            "non_unique_primary": non_unique_primary,
            "selected_dockerfile": None,
            "selected_backup": selected_backup,
            "selection_reason": "ambiguous",
            "ambiguous_candidates": considered_candidates,
            "how_to_fix": "Specify dockerfile_path/context_path explicitly.",
        }
        raise DockerfileAmbiguousError(considered_candidates, meta)

    selected = repo_root / selected_rel
    ctx = _resolve_context_path(repo_root, context_path, selected.parent)
    meta = {
        "scanned_candidates": candidates,
        "primary_candidates": primary_candidates,
        "backup_candidates": backup_candidates,
        "ignored_backups": ignored_backups,
        "non_unique_primary": non_unique_primary,
        "selected_dockerfile": selected_rel,
        "selected_backup": selected_backup,
        "selection_reason": selection_reason,
    }
    return selected, ctx, candidates, used_auto, meta


def _resolve_context_path(repo_root: Path, context_path: Optional[str], fallback: Path) -> Path:
    if context_path:
        ctx = (repo_root / context_path).resolve()
    else:
        ctx = fallback.resolve()
    try:
        ctx.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise FileNotFoundError("context_path must be under repository root") from exc
    return ctx


def _resolve_search_root(repo_root: Path, context_path: Optional[str]) -> Path:
    if not context_path:
        return repo_root.resolve()
    ctx = (repo_root / context_path).resolve()
    try:
        ctx.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise FileNotFoundError("context_path must be under repository root") from exc
    return ctx


def _find_dockerfiles(search_root: Path, repo_root: Path, search_depth: int) -> List[str]:
    candidates: List[str] = []
    for root, dirs, files in os.walk(search_root):
        rel = Path(root).relative_to(search_root)
        if len(rel.parts) > search_depth:
            dirs[:] = []
            continue
        for filename in files:
            if filename == "Dockerfile" or filename.startswith("Dockerfile."):
                found = Path(root) / filename
                rel_path = str(found.relative_to(repo_root))
                candidates.append(rel_path)
    return sorted(set(candidates))


def _is_backup_dockerfile(filename: str) -> bool:
    if filename == "Dockerfile":
        return False
    if not filename.startswith("Dockerfile."):
        return False
    remainder = filename[len("Dockerfile.") :]
    if not remainder:
        return False
    parts = [part for part in remainder.lower().split(".") if part]
    return any(part in _BACKUP_DOCKERFILE_SUFFIXES for part in parts)
