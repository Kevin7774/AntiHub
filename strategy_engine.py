from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


@dataclass
class RepoInspection:
    repo_type: str
    evidence: List[str]
    has_dockerfile: bool
    has_compose: bool


@dataclass
class StrategyDecision:
    strategy: str
    selection_reason: str
    fallback_reason: Optional[str] = None


def _walk_with_depth(root: Path, depth: int) -> Iterable[Path]:
    root = root.resolve()
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        rel = current_path.relative_to(root)
        if len(rel.parts) > depth:
            dirs[:] = []
            continue
        for name in files:
            yield current_path / name


def _collect_markers(repo_root: Path, search_depth: int) -> List[str]:
    markers: List[str] = []
    for path in _walk_with_depth(repo_root, search_depth):
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        markers.append(str(rel))
    return markers


def inspect_repo(repo_root: Path, search_depth: int = 2) -> RepoInspection:
    markers = _collect_markers(repo_root, search_depth)
    marker_set = {m.lower() for m in markers}

    def has_marker(name: str) -> bool:
        return name.lower() in marker_set

    def any_marker(names: Iterable[str]) -> bool:
        return any(name.lower() in marker_set for name in names)

    has_dockerfile = any(
        m.endswith("dockerfile") or "dockerfile." in m for m in marker_set
    )
    compose_markers = [
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ]
    has_compose = any_marker(compose_markers)
    if not has_compose:
        for marker in markers:
            path = Path(marker)
            if len(path.parts) != 1 or path.suffix not in {".yml", ".yaml"}:
                continue
            stem = path.stem.lower()
            if stem.startswith("docker-compose") or stem.startswith("compose"):
                has_compose = True
                break

    evidence: List[str] = []

    repo_type = "unknown"
    if any_marker(["project.config.json", "app.json"]):
        repo_type = "miniprogram"
        evidence.extend([
            m for m in markers if Path(m).name in {"project.config.json", "app.json"}
        ])
    elif has_marker("package.json"):
        repo_type = "node"
        evidence.extend([m for m in markers if Path(m).name in {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}])
    elif any_marker(["requirements.txt", "pyproject.toml", "setup.py", "pipfile"]):
        repo_type = "python"
        evidence.extend([m for m in markers if Path(m).name in {"requirements.txt", "pyproject.toml", "setup.py", "Pipfile"}])
    elif any_marker(["go.mod"]):
        repo_type = "go"
        evidence.extend([m for m in markers if Path(m).name == "go.mod"])
    elif any_marker(["pom.xml", "build.gradle", "build.gradle.kts"]):
        repo_type = "java"
        evidence.extend([m for m in markers if Path(m).name in {"pom.xml", "build.gradle", "build.gradle.kts"}])
    elif any_marker(["index.html", "public/index.html", "docs/index.html"]):
        repo_type = "static"
        evidence.extend([m for m in markers if m.lower().endswith("index.html")])
    elif any_marker(["mkdocs.yml", "docs/index.md", "docs/conf.py"]):
        repo_type = "docs"
        evidence.extend([m for m in markers if Path(m).name in {"mkdocs.yml", "index.md", "conf.py"}])

    if not evidence:
        # fallback to at most a few markers for diagnostics
        evidence = markers[:10]

    return RepoInspection(
        repo_type=repo_type,
        evidence=sorted(set(evidence)),
        has_dockerfile=has_dockerfile,
        has_compose=has_compose,
    )


def select_strategy(run_mode: str, inspection: RepoInspection) -> StrategyDecision:
    run_mode = (run_mode or "auto").strip().lower()
    if run_mode == "deploy":
        run_mode = "container"
    if run_mode not in {"auto", "container", "showcase", "compose"}:
        run_mode = "auto"

    if run_mode == "showcase":
        return StrategyDecision("showcase", "run_mode_showcase")

    if inspection.has_compose:
        if run_mode == "showcase":
            return StrategyDecision("showcase", "compose_found", "compose_not_supported")
        return StrategyDecision("compose", "compose_found")

    if inspection.has_dockerfile:
        return StrategyDecision("dockerfile", "dockerfile_found")

    if run_mode == "container":
        return StrategyDecision("none", "dockerfile_missing", "dockerfile_not_found")

    if inspection.repo_type in {"node", "python", "static"}:
        return StrategyDecision("generated", f"generated_for_{inspection.repo_type}")

    return StrategyDecision("showcase", "repo_type_fallback", "repo_type_not_supported")


def _read_package_json(repo_root: Path) -> dict:
    path = repo_root / "package.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def generate_dockerfile(repo_root: Path, inspection: RepoInspection) -> Tuple[Path, List[str], str]:
    generated_dir = repo_root / ".antihub" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_path = generated_dir / "Dockerfile"
    summary = ""

    if inspection.repo_type == "node":
        pkg = _read_package_json(repo_root)
        scripts = pkg.get("scripts") or {}
        start_cmd = "start" if "start" in scripts else None
        build_cmd = "build" if "build" in scripts else None
        install_cmd = "npm ci" if (repo_root / "package-lock.json").exists() else "npm install"
        if (repo_root / "yarn.lock").exists():
            install_cmd = "yarn install --frozen-lockfile"
        if (repo_root / "pnpm-lock.yaml").exists():
            install_cmd = "pnpm install --frozen-lockfile"
        run_cmd = "npm start" if start_cmd else "npm run dev"
        lines = [
            "FROM node:20-alpine",
            "WORKDIR /app",
            "COPY . .",
            f"RUN corepack enable && ({install_cmd} || npm install)",
        ]
        if build_cmd:
            lines.append("RUN npm run build")
        lines.extend([
            "EXPOSE 3000",
            f"CMD [\"sh\", \"-c\", \"{run_cmd}\"]",
        ])
        summary = f"node install={install_cmd} run={run_cmd}"
    elif inspection.repo_type == "python":
        lines = [
            "FROM python:3.11-slim",
            "WORKDIR /app",
            "ENV PYTHONDONTWRITEBYTECODE=1",
            "ENV PYTHONUNBUFFERED=1",
            "COPY . .",
            "RUN python -m pip install --upgrade pip",
            "RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; elif [ -f pyproject.toml ] || [ -f setup.py ]; then pip install --no-cache-dir .; else echo 'no dependencies'; fi",
            "EXPOSE 8000",
            "CMD [\"python\", \"-m\", \"http.server\", \"8000\"]",
        ]
        summary = "python http.server 8000"
    else:
        lines = [
            "FROM nginx:alpine",
            "WORKDIR /usr/share/nginx/html",
            "COPY . .",
            "EXPOSE 80",
        ]
        summary = "static nginx"

    dockerfile_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dockerfile_path, [str(dockerfile_path.relative_to(repo_root))], summary
