import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

IGNORED_DIRS = {".git", "node_modules", "venv", ".venv", "dist", "build"}
README_CANDIDATES = [
    "README.md",
    "README.MD",
    "README.rst",
    "README",
]

logger = logging.getLogger(__name__)

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


def _with_ssl_no_verify(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    merged = dict(env or os.environ)
    merged.setdefault("GIT_TERMINAL_PROMPT", "0")
    merged["GIT_SSL_NO_VERIFY"] = "1"
    return merged


class GitHubRateLimitError(RuntimeError):
    pass


def _safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().lower())
    return cleaned.strip("-") or "repo"


def _run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _read_readme(repo_path: Path, max_chars: int = 4000) -> str:
    for name in README_CANDIDATES:
        path = repo_path / name
        if path.exists() and path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                return text[:max_chars]
            except Exception:
                return ""
    return ""


def _file_index(repo_path: Path, max_files: int) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    count = 0
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in sorted(files):
            if count >= max_files:
                return entries
            path = Path(root) / name
            rel = str(path.relative_to(repo_path))
            try:
                stat = path.stat()
                size = stat.st_size
            except Exception:
                size = 0
            entries.append({"path": rel, "size": size, "type": "file"})
            count += 1
    return entries


def _resolve_commit(repo_path: Path) -> Optional[str]:
    code, out, _ = _run(["git", "-C", str(repo_path), "rev-parse", "HEAD"])
    if code == 0:
        return out.strip()
    return None


def _fingerprint(repo_path: Path) -> str:
    hasher = hashlib.sha1()
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in sorted(files)[:2000]:
            path = Path(root) / name
            rel = str(path.relative_to(repo_path))
            try:
                stat = path.stat()
            except Exception:
                continue
            hasher.update(rel.encode("utf-8", errors="ignore"))
            hasher.update(str(stat.st_size).encode("utf-8"))
            hasher.update(str(int(stat.st_mtime)).encode("utf-8"))
    return hasher.hexdigest()


def _parse_repo_slug(repo_url: str) -> Optional[Tuple[str, str]]:
    cleaned = repo_url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    match = re.search(r"github.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)$", cleaned)
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def _proxy_target(proxy_url: str) -> Optional[Tuple[str, int]]:
    if not proxy_url:
        return None
    cleaned = proxy_url.strip()
    if "://" not in cleaned:
        cleaned = f"http://{cleaned}"
    parsed = urlparse(cleaned)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


def _proxy_reachable(proxy_url: str, timeout: float = 0.5) -> bool:
    target = _proxy_target(proxy_url)
    if not target:
        return True
    host, port = target
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _git_env_without_unreachable_proxy() -> Optional[Dict[str, str]]:
    proxy_keys = ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"]
    env = os.environ.copy()
    proxies = [(key, env.get(key)) for key in proxy_keys if env.get(key)]
    if not proxies:
        return None
    for _, value in proxies:
        if value and not _proxy_reachable(value):
            for key, _ in proxies:
                env.pop(key, None)
            logger.warning("Proxy appears unreachable; running git without proxy.")
            return env
    return None


def _fetch_repo_meta(repo_url: str, token: Optional[str]) -> Dict[str, Any]:
    slug = _parse_repo_slug(repo_url)
    if not slug:
        return {}
    owner, repo = slug
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(api_url, headers=headers)
    try:
        with urlopen(request, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 403:
            raise GitHubRateLimitError("GITHUB_RATE_LIMIT") from exc
        return {}
    except Exception:
        return {}

    topics = data.get("topics") or []
    if not topics:
        topics_url = f"https://api.github.com/repos/{owner}/{repo}/topics"
        headers["Accept"] = "application/vnd.github+json"
        try:
            with urlopen(Request(topics_url, headers=headers), timeout=10) as resp:
                topics_data = json.loads(resp.read().decode("utf-8"))
                topics = topics_data.get("names") or []
        except Exception:
            topics = []

    return {
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "topics": topics,
        "license": (data.get("license") or {}).get("spdx_id") if data.get("license") else None,
        "default_branch": data.get("default_branch"),
    }


@dataclass
class GithubFetchResult:
    ok: bool
    output: Dict[str, Any]
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def github_fetch(payload: Dict[str, Any]) -> GithubFetchResult:
    repo_url = str(payload.get("repo_url") or "").strip()
    if not repo_url:
        return GithubFetchResult(ok=False, output={}, error_code="GIT_CLONE_FAILED", error_message="Missing repo_url")

    ref = (payload.get("ref") or "").strip() or None
    depth = int(payload.get("depth") or 1)
    include_submodules = bool(payload.get("include_submodules"))
    include_lfs = bool(payload.get("include_lfs"))
    max_files = int(payload.get("max_files") or 20000)

    workspace = Path(os.getenv("OPENCLAW_WORKSPACE", "/tmp/openclaw"))
    workspace.mkdir(parents=True, exist_ok=True)
    slug = _safe_slug(repo_url)
    repo_path = workspace / slug
    if repo_path.exists():
        shutil.rmtree(repo_path, ignore_errors=True)

    git_env = _git_env_without_unreachable_proxy()
    base_env = dict(git_env) if git_env is not None else os.environ.copy()
    effective_env = base_env
    clone_cmd = ["git", "clone", "--depth", str(max(1, depth)), repo_url, str(repo_path)]
    code, out, err = _run(clone_cmd, env=effective_env)
    if code != 0:
        combined = f"{out}\n{err}".strip()
        if _looks_like_tls_error(combined):
            logger.warning("git clone TLS error detected; retrying with GIT_SSL_NO_VERIFY=1")
            shutil.rmtree(repo_path, ignore_errors=True)
            insecure_env = _with_ssl_no_verify(base_env)
            code, out, err = _run(clone_cmd, env=insecure_env)
            if code == 0:
                effective_env = insecure_env
            else:
                return GithubFetchResult(
                    ok=False,
                    output={},
                    error_code="GIT_CLONE_FAILED",
                    error_message=(err or out).strip() or "git clone failed",
                )
        else:
            return GithubFetchResult(
                ok=False,
                output={},
                error_code="GIT_CLONE_FAILED",
                error_message=err.strip() or "git clone failed",
            )

    if ref:
        code, _, err = _run(["git", "-C", str(repo_path), "checkout", ref], env=effective_env)
        if code != 0:
            return GithubFetchResult(
                ok=False,
                output={},
                error_code="GIT_CLONE_FAILED",
                error_message=err.strip() or "git checkout failed",
            )

    if include_submodules:
        code, _, err = _run(
            ["git", "-C", str(repo_path), "submodule", "update", "--init", "--recursive"],
            env=effective_env,
        )
        if code != 0:
            return GithubFetchResult(
                ok=False,
                output={},
                error_code="SUBMODULE_FAILED",
                error_message=err.strip() or "git submodule failed",
            )

    if include_lfs:
        code, _, err = _run(["git", "-C", str(repo_path), "lfs", "pull"], env=effective_env)
        if code != 0:
            return GithubFetchResult(
                ok=False,
                output={},
                error_code="LFS_FAILED",
                error_message=err.strip() or "git lfs pull failed",
            )

    commit_sha = _resolve_commit(repo_path)
    if not commit_sha:
        fingerprint = _fingerprint(repo_path)
        commit_sha = f"snapshot-{fingerprint[:12]}"

    readme_rendered = _read_readme(repo_path)

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_TOKEN")
    repo_meta: Dict[str, Any] = {}
    repo_meta_available = True
    repo_meta_reason: Optional[str] = None
    try:
        repo_meta = _fetch_repo_meta(repo_url, token)
        if not repo_meta:
            repo_meta_available = False
            repo_meta_reason = "unavailable"
    except GitHubRateLimitError:
        logger.warning("GitHub API rate limit reached; proceeding without repo metadata.")
        repo_meta_available = False
        repo_meta_reason = "rate_limit"
        repo_meta = {}

    file_index = _file_index(repo_path, max_files=max_files)

    ingest_meta = {
        "generated_at": time.time(),
        "repo_url": repo_url,
        "ref": ref,
        "commit_sha": commit_sha,
        "repo_path": str(repo_path),
        "repo_meta": repo_meta,
        "repo_meta_available": repo_meta_available,
        "repo_meta_reason": repo_meta_reason,
        "readme_rendered": readme_rendered,
        "file_index_count": len(file_index),
        "max_files": max_files,
    }

    meta_dir = repo_path / ".antihub"
    meta_dir.mkdir(parents=True, exist_ok=True)
    ingest_meta_path = meta_dir / "ingest_meta.json"
    ingest_meta_path.write_text(json.dumps(ingest_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return GithubFetchResult(
        ok=True,
        output={
            "repo_path": str(repo_path),
            "commit_sha": commit_sha,
            "file_index": file_index,
            "readme_rendered": readme_rendered,
            "repo_meta": repo_meta,
            "repo_meta_available": repo_meta_available,
            "repo_meta_reason": repo_meta_reason,
            "ingest_meta": ingest_meta,
            "ingest_meta_path": str(ingest_meta_path),
        },
    )
