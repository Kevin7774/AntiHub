import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from config import DEFAULT_BRANCH, GIT_CLONE_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

@dataclass
class CloneResult:
    requested_ref: Optional[str]
    resolved_ref: Optional[str]
    default_branch: Optional[str]
    used_fallback: bool


class GitRefNotFoundError(RuntimeError):
    def __init__(
        self,
        message: str,
        requested_ref: Optional[str] = None,
        default_branch: Optional[str] = None,
        heads: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.requested_ref = requested_ref
        self.default_branch = default_branch
        self.heads = heads or []


BRANCH_NOT_FOUND_PATTERN = re.compile(r"Remote branch .* not found", re.IGNORECASE)
SYMBOLIC_REF_PATTERN = re.compile(r"^ref:\s+refs/heads/(?P<branch>\S+)\s+HEAD$")
TLS_ERROR_HINTS = (
    "gnutls",
    "tls",
    "ssl",
    "certificate",
    "schannel",
    "handshake",
    "recv error",
)


def _looks_like_tls_error(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in TLS_ERROR_HINTS)


def _with_ssl_no_verify(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    merged = dict(env or os.environ)
    merged.setdefault("GIT_TERMINAL_PROMPT", "0")
    merged["GIT_SSL_NO_VERIFY"] = "1"
    return merged


def normalize_ref(ref: Optional[str]) -> Optional[str]:
    if ref is None:
        return None
    value = ref.strip()
    if not value:
        return None
    if value.lower() == "auto":
        return "auto"
    return value


def _is_local_repo(repo_url: str) -> Optional[Path]:
    if repo_url.startswith("file://"):
        return Path(repo_url[len("file://") :])
    local_path = Path(repo_url)
    if local_path.exists() and local_path.is_dir():
        return local_path
    return None


def detect_default_branch(repo_url: str) -> Optional[str]:
    if _is_local_repo(repo_url):
        return None
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", repo_url, "HEAD"],
            check=True,
            text=True,
            capture_output=True,
            timeout=GIT_CLONE_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("ref:"):
            continue
        match = SYMBOLIC_REF_PATTERN.match(line)
        if match:
            return match.group("branch")
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
            return parts[1].split("refs/heads/", 1)[1]
    return None


def list_remote_heads(repo_url: str, limit: int = 20) -> List[str]:
    if _is_local_repo(repo_url):
        return []
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", repo_url],
            check=True,
            text=True,
            capture_output=True,
            timeout=GIT_CLONE_TIMEOUT_SECONDS,
        )
    except Exception:
        return []
    heads: List[str] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if ref.startswith("refs/heads/"):
            heads.append(ref.split("refs/heads/", 1)[1])
        if len(heads) >= limit:
            break
    return heads


def _git_clone(
    repo_url: str,
    branch: str,
    target_dir: Path,
    env: Optional[Dict[str, str]] = None,
) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            repo_url,
            str(target_dir),
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=GIT_CLONE_TIMEOUT_SECONDS,
        env=env,
    )


def clone_repo(
    repo_url: str,
    ref: Optional[str],
    target_dir: Path,
    enable_submodules: bool = False,
    enable_lfs: bool = False,
) -> CloneResult:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    requested_ref = normalize_ref(ref)
    local_path = _is_local_repo(repo_url)
    if local_path:
        shutil.copytree(local_path, target_dir, ignore=shutil.ignore_patterns(".git"))
        return CloneResult(requested_ref, requested_ref, None, False)

    def run_clone(branch: str, retries: int = 2) -> None:
        last_exc: Optional[subprocess.CalledProcessError] = None
        git_env: Optional[Dict[str, str]] = None
        for attempt in range(retries + 1):
            try:
                _git_clone(repo_url, branch, target_dir, env=git_env)
                _post_clone_prepare(target_dir, enable_submodules, enable_lfs, env=git_env)
                return
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "") + (exc.stdout or "")
                if BRANCH_NOT_FOUND_PATTERN.search(stderr):
                    raise
                if git_env is None and _looks_like_tls_error(stderr):
                    git_env = _with_ssl_no_verify()
                    logger.warning("git clone TLS error detected; retrying with GIT_SSL_NO_VERIFY=1")
                    continue
                last_exc = exc
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise last_exc

    if requested_ref in {None, "auto"}:
        default_branch = detect_default_branch(repo_url)
        candidates = [default_branch] if default_branch else ["main", "master"]
        last_not_found: Optional[subprocess.CalledProcessError] = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                run_clone(candidate)
                return CloneResult(requested_ref, candidate, default_branch, False)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "") + (exc.stdout or "")
                if BRANCH_NOT_FOUND_PATTERN.search(stderr):
                    last_not_found = exc
                    continue
                raise
        if last_not_found is not None:
            heads = list_remote_heads(repo_url, limit=20)
            suggestion = default_branch or DEFAULT_BRANCH
            heads_text = ", ".join(heads) if heads else "N/A"
            message = (
                "Requested ref not found."
                f" requested_ref={requested_ref}"
                f" default_branch={default_branch or 'unknown'}"
                f" suggestion=use '{suggestion}' or leave ref empty/auto"
                f" heads={heads_text}"
            )
            raise GitRefNotFoundError(
                message,
                requested_ref=requested_ref,
                default_branch=default_branch,
                heads=heads,
            ) from last_not_found
        return CloneResult(requested_ref, default_branch, default_branch, False)

    try:
        run_clone(requested_ref)
        return CloneResult(requested_ref, requested_ref, None, False)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "") + (exc.stdout or "")
        if BRANCH_NOT_FOUND_PATTERN.search(stderr):
            default_branch = detect_default_branch(repo_url)
            can_fallback = (
                isinstance(requested_ref, str)
                and requested_ref.lower() in {"master", "main"}
            )
            if can_fallback and default_branch and default_branch != requested_ref:
                try:
                    run_clone(default_branch)
                    return CloneResult(requested_ref, default_branch, default_branch, True)
                except subprocess.CalledProcessError:
                    pass
            heads = list_remote_heads(repo_url, limit=20)
            suggestion = default_branch or DEFAULT_BRANCH
            heads_text = ", ".join(heads) if heads else "N/A"
            message = (
                "Requested ref not found."
                f" requested_ref={requested_ref}"
                f" default_branch={default_branch or 'unknown'}"
                f" suggestion=use '{suggestion}' or leave ref empty/auto"
                f" heads={heads_text}"
            )
            raise GitRefNotFoundError(
                message,
                requested_ref=requested_ref,
                default_branch=default_branch,
                heads=heads,
            ) from exc
        raise


def _post_clone_prepare(
    repo_dir: Path,
    enable_submodules: bool,
    enable_lfs: bool,
    env: Optional[Dict[str, str]] = None,
) -> None:
    if enable_submodules and (repo_dir / ".gitmodules").exists():
        subprocess.run(
            ["git", "-C", str(repo_dir), "submodule", "update", "--init", "--recursive"],
            check=True,
            text=True,
            capture_output=True,
            timeout=GIT_CLONE_TIMEOUT_SECONDS,
            env=env,
        )
    if enable_lfs and has_lfs(repo_dir):
        subprocess.run(
            ["git", "-C", str(repo_dir), "lfs", "pull"],
            check=True,
            text=True,
            capture_output=True,
            timeout=GIT_CLONE_TIMEOUT_SECONDS,
            env=env,
        )


def has_lfs(repo_dir: Path) -> bool:
    attrs = repo_dir / ".gitattributes"
    if attrs.exists():
        try:
            text = attrs.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        if "filter=lfs" in text:
            return True
    for root, _, files in os.walk(repo_dir):
        rel = Path(root).relative_to(repo_dir)
        if len(rel.parts) > 2:
            break
        for file in files:
            if file.endswith(".gitattributes"):
                return True
    return False
