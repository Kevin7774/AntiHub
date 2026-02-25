import json
import re
import time
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from config import (
    ANALYZE_README_MAX_CHARS,
    ANALYZE_SNIPPET_MAX_CHARS,
    ANALYZE_TREE_DEPTH,
    ANALYZE_TREE_MAX_ENTRIES,
)

HARD_TREE_DEPTH = 3
HARD_MAX_ENTRIES = 400
HARD_SNIPPET_MAX_CHARS = 20000

README_CANDIDATES = ["README.md", "README.MD", "README"]
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "dist",
    "build",
    ".tox",
}

CONFIG_FILE_NAMES = {
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "Makefile",
    ".env.example",
    ".env.sample",
    ".env",
    "config.yaml",
    "config.yml",
}

ENV_LINE_PATTERN = re.compile(r"^\s*([A-Z][A-Z0-9_]{1,})\s*[:=]")
ENV_INLINE_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b\s*=")
PORT_PATTERN = re.compile(r"(?:port|端口|localhost|0\.0\.0\.0|127\.0\.0\.1)\D{0,10}(\d{2,5})", re.IGNORECASE)

SECRET_PAIR_PATTERN = re.compile(
    r"(?P<key>[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
SECRET_JSON_PATTERN = re.compile(
    r"(?P<key>\"[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\"\s*:\s*)\"(?P<value>[^\"]+)\"",
    re.IGNORECASE,
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._\-~+/]+=*)")
SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
MARKDOWN_FENCE_PATTERN = re.compile(r"```[\s\S]*?```")
MARKDOWN_INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")


def sanitize_text(text: str) -> str:
    if not text:
        return ""
    sanitized = SECRET_PAIR_PATTERN.sub(lambda m: f"{m.group('key')}{m.group('sep')}***", text)
    sanitized = SECRET_JSON_PATTERN.sub(lambda m: f"{m.group('key')}\"***\"", sanitized)
    sanitized = BEARER_PATTERN.sub("Bearer ***", sanitized)
    sanitized = SK_PATTERN.sub("sk-***", sanitized)
    sanitized = JWT_PATTERN.sub("***", sanitized)
    return sanitized


def to_plain_text(text: str, max_chars: Optional[int] = None) -> str:
    if not text:
        return ""
    cleaned = str(text)
    cleaned = MARKDOWN_FENCE_PATTERN.sub(" ", cleaned)
    cleaned = MARKDOWN_INLINE_CODE_PATTERN.sub(lambda m: m.group(1), cleaned)
    cleaned = MARKDOWN_IMAGE_PATTERN.sub(lambda m: m.group(1) or " ", cleaned)
    cleaned = MARKDOWN_LINK_PATTERN.sub(lambda m: m.group(1), cleaned)
    cleaned = HTML_TAG_PATTERN.sub(" ", cleaned)
    cleaned = unescape(cleaned)
    cleaned = sanitize_text(cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    if max_chars and max_chars > 0 and len(cleaned) > max_chars:
        return cleaned[:max_chars]
    return cleaned


def _extract_env_keys_from_text(text: str) -> List[str]:
    keys: List[str] = []
    for line in text.splitlines():
        match = ENV_LINE_PATTERN.match(line)
        if match:
            key = match.group(1)
            if key and key not in keys:
                keys.append(key)
        for inline in ENV_INLINE_PATTERN.findall(line):
            if inline and inline not in keys:
                keys.append(inline)
    return keys


def _extract_env_keys_from_file(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    keys = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        match = ENV_LINE_PATTERN.match(line)
        if match:
            key = match.group(1)
            if key and key not in keys:
                keys.append(key)
    return keys


def _read_file_excerpt(path: Path, max_chars: int) -> Tuple[str, bool]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", False
    truncated = len(text) > max_chars
    return text[:max_chars], truncated


def _read_readme(repo_path: Path, max_chars: int) -> Tuple[Optional[str], str, bool]:
    for name in README_CANDIDATES:
        candidate = repo_path / name
        if candidate.exists():
            text, truncated = _read_file_excerpt(candidate, max_chars)
            return candidate.name, text, truncated
    for candidate in sorted(repo_path.glob("README.*")):
        if candidate.is_file():
            text, truncated = _read_file_excerpt(candidate, max_chars)
            return candidate.name, text, truncated
    return None, "", False


def _parse_readme(text: str) -> Dict[str, Any]:
    headings: List[str] = []
    bullets: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading and heading not in headings:
                headings.append(heading)
            continue
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if item and item not in bullets:
                bullets.append(item)
            continue
        if re.match(r"\d+\.\s+", line):
            item = re.sub(r"^\d+\.\s+", "", line).strip()
            if item and item not in bullets:
                bullets.append(item)
    return {"headings": headings, "bullets": bullets}


def _build_tree(repo_path: Path, max_depth: int, max_entries: int) -> Tuple[List[str], int, bool]:
    entries: List[str] = []
    truncated = False

    def walk(current: Path, depth: int, prefix: str) -> None:
        nonlocal truncated
        if depth > max_depth or truncated:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except Exception:
            return
        for child in children:
            if child.name in IGNORED_DIRS:
                continue
            if child.is_dir() and child.name.startswith("."):
                continue
            if child.is_file() and child.name.startswith(".") and child.name not in CONFIG_FILE_NAMES:
                continue
            line = f"{prefix}{child.name}{'/' if child.is_dir() else ''}"
            entries.append(line)
            if len(entries) >= max_entries:
                truncated = True
                return
            if child.is_dir():
                walk(child, depth + 1, prefix + "  ")
                if truncated:
                    return

    walk(repo_path, 1, "")
    return entries, len(entries), truncated


def _find_config_files(repo_path: Path, max_depth: int, max_entries: int) -> List[str]:
    found: List[str] = []
    entries, _, _ = _build_tree(repo_path, max_depth, max_entries)
    for entry in entries:
        name = entry.strip().rstrip("/")
        if name in CONFIG_FILE_NAMES and name not in found:
            found.append(name)
    return found


def _parse_dockerfile(dockerfile_path: Path, max_chars: int) -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": dockerfile_path.name}
    expose_ports: List[int] = []
    cmd = None
    entrypoint = None
    workdir = None
    env_keys: List[str] = []
    try:
        text = dockerfile_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return info
    excerpt = sanitize_text(text[:max_chars])
    info["excerpt"] = excerpt
    info["truncated"] = len(text) > max_chars
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("EXPOSE "):
            rest = line.split(None, 1)[1]
            for token in rest.replace("/tcp", "").replace("/udp", "").split():
                if token.isdigit():
                    port = int(token)
                    if port not in expose_ports:
                        expose_ports.append(port)
        if upper.startswith("CMD "):
            cmd = sanitize_text(line[4:].strip())
        if upper.startswith("ENTRYPOINT "):
            entrypoint = sanitize_text(line[len("ENTRYPOINT ") :].strip())
        if upper.startswith("WORKDIR "):
            workdir = sanitize_text(line[len("WORKDIR ") :].strip())
        if upper.startswith("ENV ") or upper.startswith("ARG "):
            env_keys.extend(_extract_env_keys_from_text(line))
    if expose_ports:
        info["expose"] = expose_ports
    if cmd:
        info["cmd"] = cmd
    if entrypoint:
        info["entrypoint"] = entrypoint
    if workdir:
        info["workdir"] = workdir
    if env_keys:
        info["env_keys"] = sorted(set(env_keys))
    return info


def _parse_compose(compose_path: Path, max_chars: int) -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": compose_path.name}
    try:
        raw_text = compose_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return info
    info["excerpt"] = sanitize_text(raw_text[:max_chars])
    info["truncated"] = len(raw_text) > max_chars
    try:
        data = yaml.safe_load(raw_text)
    except Exception:
        return info
    if not isinstance(data, dict):
        return info
    services = data.get("services")
    if not isinstance(services, dict):
        return info
    service_names = sorted([str(key) for key in services.keys()])
    ports: List[int] = []
    for service in services.values():
        if not isinstance(service, dict):
            continue
        raw_ports = service.get("ports") or []
        if not isinstance(raw_ports, list):
            continue
        for item in raw_ports:
            if isinstance(item, int):
                ports.append(item)
                continue
            if not isinstance(item, str):
                continue
            parts = item.split(":")
            if parts and parts[0].isdigit():
                ports.append(int(parts[0]))
            elif len(parts) > 1 and parts[1].isdigit():
                ports.append(int(parts[1]))
    if service_names:
        info["services"] = service_names
    if ports:
        info["ports"] = sorted(set(ports))
    return info


def _parse_package_json(package_path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(package_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    scripts = data.get("scripts") or {}
    if isinstance(scripts, dict):
        return {"scripts": sorted([str(key) for key in scripts.keys()])}
    return {}


def _extract_ports_from_text(text: str) -> List[int]:
    ports: List[int] = []
    for match in PORT_PATTERN.findall(text):
        try:
            value = int(match)
        except ValueError:
            continue
        if value not in ports:
            ports.append(value)
    return ports


def extract_signals(
    repo_path: Path,
    env_keys: List[str],
    max_depth: Optional[int] = None,
    max_entries: Optional[int] = None,
    readme_max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    depth = max_depth if max_depth is not None else ANALYZE_TREE_DEPTH
    entries_limit = max_entries if max_entries is not None else ANALYZE_TREE_MAX_ENTRIES
    readme_limit = readme_max_chars if readme_max_chars is not None else ANALYZE_README_MAX_CHARS
    depth = min(depth, HARD_TREE_DEPTH)
    entries_limit = min(entries_limit, HARD_MAX_ENTRIES)
    snippet_limit = min(readme_limit, ANALYZE_SNIPPET_MAX_CHARS, HARD_SNIPPET_MAX_CHARS)

    readme_name, readme_text, readme_truncated = _read_readme(repo_path, snippet_limit)
    sanitized_readme = sanitize_text(readme_text)
    readme_meta = _parse_readme(sanitized_readme)
    readme_env_keys = _extract_env_keys_from_text(sanitized_readme)

    tree_entries, tree_count, tree_truncated = _build_tree(repo_path, depth, entries_limit)
    config_files = _find_config_files(repo_path, depth, entries_limit)

    dockerfile_path = repo_path / "Dockerfile"
    docker_info = _parse_dockerfile(dockerfile_path, snippet_limit) if dockerfile_path.exists() else {}

    compose_files = [repo_path / name for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")]
    compose_info_list = [
        _parse_compose(path, snippet_limit) for path in compose_files if path.exists()
    ]

    package_info = _parse_package_json(repo_path / "package.json") if (repo_path / "package.json").exists() else {}

    env_from_examples: List[str] = []
    for name in (".env.example", ".env.sample"):
        path = repo_path / name
        if path.exists():
            env_from_examples.extend(_extract_env_keys_from_file(path))

    env_candidates = sorted({*(env_keys or []), *readme_env_keys, *env_from_examples, *docker_info.get("env_keys", [])})

    port_hints: List[int] = []
    port_hints.extend(_extract_ports_from_text(sanitized_readme))
    port_hints.extend(docker_info.get("expose") or [])
    for compose_info in compose_info_list:
        port_hints.extend(compose_info.get("ports") or [])
    port_hints = sorted({port for port in port_hints if isinstance(port, int)})

    signals: Dict[str, Any] = {
        "generated_at": time.time(),
        "meta": {
            "truncation": {
                "readme_truncated": readme_truncated,
                "dockerfile_truncated": bool(docker_info.get("truncated")) if docker_info else False,
                "compose_truncated": any(info.get("truncated") for info in compose_info_list),
                "tree_truncated": tree_truncated,
                "readme_limit": snippet_limit,
                "tree_depth_limit": depth,
                "tree_entries_limit": entries_limit,
            }
        },
        "readme": {
            "path": readme_name,
            "excerpt": sanitized_readme,
            "headings": readme_meta.get("headings") or [],
            "bullets": readme_meta.get("bullets") or [],
        },
        "tree": {
            "depth": depth,
            "entries": tree_entries,
            "count": tree_count,
            "truncated": tree_truncated,
        },
        "files": {
            "config_files": config_files,
            "dockerfile": docker_info.get("path") if docker_info else None,
            "compose_files": [info.get("path") for info in compose_info_list if info.get("path")],
            "has_requirements": (repo_path / "requirements.txt").exists(),
            "has_pyproject": (repo_path / "pyproject.toml").exists(),
            "has_package_json": (repo_path / "package.json").exists(),
            "has_makefile": (repo_path / "Makefile").exists(),
        },
        "docker": docker_info,
        "compose": compose_info_list,
        "package": package_info,
        "ports": port_hints,
        "env_keys": env_candidates,
    }
    return signals
