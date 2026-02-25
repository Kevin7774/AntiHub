import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from analyze.signals import IGNORED_DIRS, extract_signals, sanitize_text, to_plain_text
from config import (
    VISUAL_GRAPH_MAX_NODES,
    VISUAL_LANGUAGE_MAX_FILES,
    VISUAL_SPOTLIGHT_MAX_CHARS,
    VISUAL_SPOTLIGHT_MAX_FILES,
    VISUAL_TEMPLATE_VERSION,
    VISUAL_TREE_DEPTH,
    VISUAL_TREE_MAX_ENTRIES,
)
from evidence import evidence_strength_rank, make_evidence, validate_evidence
from visualize.knowledge_graph import build_knowledge_graph  # noqa: F401

LANGUAGE_BY_EXTENSION = {
    ".py": "Python",
    ".pyw": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".h": "C/C++",
    ".hpp": "C++",
    ".swift": "Swift",
    ".scala": "Scala",
    ".lua": "Lua",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".md": "Markdown",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
}

ENTRYPOINT_CANDIDATES = [
    "main.py",
    "app.py",
    "server.py",
    "index.py",
    "cli.py",
    "wsgi.py",
    "manage.py",
    "main.js",
    "index.js",
    "app.js",
    "server.js",
    "main.ts",
    "index.ts",
    "main.tsx",
    "index.tsx",
]

SPOTLIGHT_PRIORITIES = [
    "README.md",
    "README.MD",
    "README",
    "main.py",
    "app.py",
    "server.py",
    "index.py",
    "main.js",
    "index.js",
    "app.js",
    "server.js",
    "main.ts",
    "index.ts",
    "main.tsx",
    "index.tsx",
]


def _detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    return LANGUAGE_BY_EXTENSION.get(ext, ext.lstrip(".").upper() or "Text")


def _safe_read_text(path: Path, max_chars: int) -> Tuple[str, bool]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", False
    truncated = len(text) > max_chars
    snippet = text[:max_chars]
    return sanitize_text(snippet), truncated


def _summarize_readme(text: str, max_chars: int = 600) -> Dict[str, Any]:
    cleaned = to_plain_text(text or "").strip()
    if not cleaned:
        return {"text": "", "truncated": False}
    truncated = len(cleaned) > max_chars
    return {"text": cleaned[:max_chars], "truncated": truncated}


def _scan_languages(repo_path: Path, max_files: int) -> List[Dict[str, Any]]:
    stats: Dict[str, Dict[str, int]] = {}
    count = 0
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in files:
            path = Path(root) / name
            ext = path.suffix.lower()
            if ext not in LANGUAGE_BY_EXTENSION:
                continue
            lang = LANGUAGE_BY_EXTENSION[ext]
            try:
                size = path.stat().st_size
            except Exception:
                size = 0
            if lang not in stats:
                stats[lang] = {"files": 0, "bytes": 0}
            stats[lang]["files"] += 1
            stats[lang]["bytes"] += int(size)
            count += 1
            if count >= max_files:
                break
        if count >= max_files:
            break
    items = [
        {"name": lang, "files": data["files"], "bytes": data["bytes"]}
        for lang, data in stats.items()
    ]
    items.sort(key=lambda item: (item["bytes"], item["files"]), reverse=True)
    return items


def _parse_requirements(requirements_path: Path, max_items: int = 50) -> List[str]:
    items: List[str] = []
    try:
        text = requirements_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return items
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            continue
        name = line.split(";")[0].strip()
        name = name.split("==")[0].strip()
        if name and name not in items:
            items.append(name)
        if len(items) >= max_items:
            break
    return items


def _parse_pyproject(pyproject_path: Path, max_items: int = 50) -> List[str]:
    try:
        import tomllib  # type: ignore
    except Exception:
        return []
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    deps: List[str] = []
    project = data.get("project") if isinstance(data, dict) else {}
    if isinstance(project, dict):
        for item in project.get("dependencies") or []:
            if isinstance(item, str):
                deps.append(item.split(";")[0].split("==")[0].strip())
    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data, dict) else {}
    if isinstance(poetry, dict):
        for key in (poetry.get("dependencies") or {}).keys():
            if key and key not in deps and key not in {"python"}:
                deps.append(str(key))
    cleaned = [dep for dep in deps if dep]
    return cleaned[:max_items]


def _parse_package_json(package_path: Path, max_items: int = 50) -> List[str]:
    try:
        data = json.loads(package_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    deps: List[str] = []
    if isinstance(data, dict):
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            values = data.get(section)
            if isinstance(values, dict):
                for key in values.keys():
                    if key not in deps:
                        deps.append(str(key))
            if len(deps) >= max_items:
                break
    return deps[:max_items]


def _parse_package_entrypoints(package_path: Path) -> List[str]:
    try:
        data = json.loads(package_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    entrypoints: List[str] = []
    if isinstance(data, dict):
        main = data.get("main")
        if isinstance(main, str):
            entrypoints.append(main)
        bin_value = data.get("bin")
        if isinstance(bin_value, str):
            entrypoints.append(bin_value)
        elif isinstance(bin_value, dict):
            for value in bin_value.values():
                if isinstance(value, str):
                    entrypoints.append(value)
    return entrypoints


def _detect_entrypoints(repo_path: Path, file_index: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    entrypoints: List[Dict[str, Any]] = []
    seen: set[str] = set()

    if file_index:
        for item in file_index:
            path = str(item.get("path") or "")
            if path in ENTRYPOINT_CANDIDATES:
                entrypoints.append({"path": path, "kind": "file", "reason": "well_known"})
                seen.add(path)
    else:
        for name in ENTRYPOINT_CANDIDATES:
            path = repo_path / name
            if path.exists() and path.is_file():
                entrypoints.append({"path": name, "kind": "file", "reason": "well_known"})
                seen.add(name)

    package_path = repo_path / "package.json"
    if package_path.exists():
        for entry in _parse_package_entrypoints(package_path):
            if entry and entry not in seen:
                entrypoints.append({"path": entry, "kind": "node", "reason": "package.json"})
                seen.add(entry)

    return entrypoints


def _repo_name_from_url(repo_url: Optional[str]) -> str:
    value = str(repo_url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    path = parsed.path or value
    parts = [item for item in path.rstrip("/").split("/") if item]
    if not parts:
        return ""
    name = parts[-1]
    if name.endswith(".git"):
        name = name[: -4]
    name = sanitize_text(name).strip()
    return name


def build_repo_index(
    repo_path: Path,
    repo_url: Optional[str] = None,
    env_keys: Optional[List[str]] = None,
    commit_sha: Optional[str] = None,
    template_version: Optional[str] = None,
    ingest_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    signals = extract_signals(
        repo_path,
        env_keys=env_keys or [],
        max_depth=VISUAL_TREE_DEPTH,
        max_entries=VISUAL_TREE_MAX_ENTRIES,
        readme_max_chars=VISUAL_SPOTLIGHT_MAX_CHARS,
    )
    tree = signals.get("tree") or {}
    readme_info = signals.get("readme") or {}
    readme_path = readme_info.get("path")
    readme_excerpt = readme_info.get("excerpt") or ""
    readme_lines = readme_excerpt.splitlines() if readme_excerpt else []
    readme_line_range = None
    if readme_lines:
        readme_line_range = {"start": 1, "end": len(readme_lines)}
    languages = _scan_languages(repo_path, VISUAL_LANGUAGE_MAX_FILES)
    dependencies = {
        "python": [],
        "node": [],
    }
    requirements_path = repo_path / "requirements.txt"
    if requirements_path.exists():
        for dep in _parse_requirements(requirements_path):
            dependencies["python"].append({"name": dep, "source": "requirements.txt"})
    pyproject_path = repo_path / "pyproject.toml"
    if pyproject_path.exists():
        for dep in _parse_pyproject(pyproject_path):
            if dep and dep not in [item.get("name") for item in dependencies["python"]]:
                dependencies["python"].append({"name": dep, "source": "pyproject.toml"})
    package_path = repo_path / "package.json"
    if package_path.exists():
        for dep in _parse_package_json(package_path):
            dependencies["node"].append({"name": dep, "source": "package.json"})
    ports = signals.get("ports") or []
    file_index = None
    readme_rendered = None
    repo_meta = None
    if ingest_meta and isinstance(ingest_meta, dict):
        file_index = ingest_meta.get("file_index")
        readme_rendered = ingest_meta.get("readme_rendered")
        repo_meta = ingest_meta.get("repo_meta")
    repo_name = _repo_name_from_url(repo_url) or sanitize_text(repo_path.name).strip() or "repository"
    readme_summary = _summarize_readme(readme_rendered or readme_excerpt)
    if readme_path:
        readme_summary["path"] = readme_path
    if readme_line_range:
        readme_summary["line_range"] = readme_line_range
    entrypoints = _detect_entrypoints(repo_path, file_index if isinstance(file_index, list) else None)
    return {
        "generated_at": time.time(),
        "template_version": template_version or VISUAL_TEMPLATE_VERSION,
        "repo_name": repo_name,
        "repo_slug": sanitize_text(repo_path.name).strip(),
        "repo_url": repo_url,
        "commit_sha": commit_sha,
        "tree": tree,
        "languages": languages,
        "dependencies": dependencies,
        "ports": ports,
        "entrypoints": entrypoints,
        "readme_summary": readme_summary,
        "repo_meta": repo_meta or {},
        "config_files": (signals.get("files") or {}).get("config_files") or [],
        "meta": {
            "limits": {
                "tree_depth": VISUAL_TREE_DEPTH,
                "tree_entries": VISUAL_TREE_MAX_ENTRIES,
                "spotlight_chars": VISUAL_SPOTLIGHT_MAX_CHARS,
                "language_files": VISUAL_LANGUAGE_MAX_FILES,
            }
        },
    }


def build_repo_graph(
    repo_index: Dict[str, Any],
    max_nodes: Optional[int] = None,
) -> Dict[str, Any]:
    entries = ((repo_index.get("tree") or {}).get("entries") or [])[:]
    repo_name = str(repo_index.get("repo_name") or "repo")
    max_nodes = max_nodes or VISUAL_GRAPH_MAX_NODES
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    root_id = "root"
    nodes.append({"id": root_id, "label": repo_name, "type": "root", "depth": 0})
    stack: List[str] = [root_id]
    truncated = False
    for raw in entries:
        if len(nodes) >= max_nodes:
            truncated = True
            break
        if not isinstance(raw, str):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        depth = indent // 2 + 1
        name = raw.strip()
        is_dir = name.endswith("/")
        name = name.rstrip("/")
        if not name:
            continue
        while len(stack) > depth:
            stack.pop()
        parent = stack[-1] if stack else root_id
        node_id = f"{parent}/{name}" if parent != root_id else name
        nodes.append(
            {
                "id": node_id,
                "label": name,
                "type": "dir" if is_dir else "file",
                "depth": depth,
            }
        )
        edges.append({"source": parent, "target": node_id})
        stack.append(node_id)

    layout_width = 1200
    layout_height = 700
    depth_groups: Dict[int, List[Dict[str, Any]]] = {}
    for node in nodes:
        depth_groups.setdefault(int(node.get("depth", 0)), []).append(node)
    max_depth = max(depth_groups.keys()) if depth_groups else 0
    for depth, items in depth_groups.items():
        count = len(items)
        if count == 1:
            items[0]["x"] = 0
        else:
            span = layout_width
            step = span / (count - 1)
            for idx, item in enumerate(items):
                item["x"] = -layout_width / 2 + step * idx
        y = -layout_height / 2 + (layout_height / max(1, max_depth + 1)) * depth
        for item in items:
            item["y"] = y

    return {
        "generated_at": time.time(),
        "template_version": repo_index.get("template_version"),
        "nodes": nodes,
        "edges": edges,
        "meta": {"truncated": truncated, "max_nodes": max_nodes},
    }


def _extract_highlights(lines: List[str]) -> List[Dict[str, Any]]:
    highlight_lines: List[int] = []
    pattern = re.compile(r"\\b(def|class|function|export|interface|type|const|let|var|module\\.exports)\\b")
    for idx, line in enumerate(lines, start=1):
        if pattern.search(line):
            highlight_lines.append(idx)
    ranges: List[Dict[str, Any]] = []
    if not highlight_lines:
        return ranges
    start = prev = highlight_lines[0]
    for line_no in highlight_lines[1:]:
        if line_no == prev + 1:
            prev = line_no
            continue
        ranges.append({"start_line": start, "end_line": prev, "reason": "declaration"})
        start = prev = line_no
    ranges.append({"start_line": start, "end_line": prev, "reason": "declaration"})
    return ranges


def select_spotlights(
    repo_path: Path,
    max_files: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    max_files = max_files or VISUAL_SPOTLIGHT_MAX_FILES
    max_chars = max_chars or VISUAL_SPOTLIGHT_MAX_CHARS
    selected: List[Path] = []
    for name in SPOTLIGHT_PRIORITIES:
        path = repo_path / name
        if path.exists() and path.is_file():
            selected.append(path)
    if len(selected) < max_files:
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for file in files:
                path = Path(root) / file
                if path in selected:
                    continue
                ext = path.suffix.lower()
                if ext and ext in LANGUAGE_BY_EXTENSION:
                    selected.append(path)
                if len(selected) >= max_files:
                    break
            if len(selected) >= max_files:
                break

    snippets: List[Dict[str, Any]] = []
    for path in selected[:max_files]:
        rel = str(path.relative_to(repo_path))
        snippet, truncated = _safe_read_text(path, max_chars)
        if not snippet:
            continue
        lines = snippet.splitlines()
        line_range = {"start": 1, "end": len(lines) if lines else 1}
        highlights = _extract_highlights(lines)
        explanation = "Overview snippet for code review."
        if highlights:
            explanation = f"Key declarations detected: {len(highlights)} block(s)."
        explanation = sanitize_text(explanation)
        strength = "strong" if highlights else "medium"
        evidence = make_evidence(
            "code",
            [{"kind": "file", "file": rel, "line_range": line_range}],
            "spotlight_snippet",
            strength,
        )
        snippets.append(
            {
                "file_path": rel,
                "language": _detect_language(path),
                "start_line": 1,
                "end_line": len(lines),
                "line_range": line_range,
                "highlights": highlights,
                "explanation": explanation,
                "truncated": truncated,
                "snippet": snippet,
                "evidence": evidence,
            }
        )

    return {
        "generated_at": time.time(),
        "items": snippets,
        "meta": {"max_files": max_files, "max_chars": max_chars},
    }


def build_storyboard(
    repo_index: Dict[str, Any],
    repo_graph: Dict[str, Any],
    spotlights: Dict[str, Any],
    template_version: Optional[str] = None,
) -> Dict[str, Any]:
    repo_name = repo_index.get("repo_name") or "Repository"
    ports = repo_index.get("ports") or []
    tree_entries = (repo_index.get("tree") or {}).get("entries") or []
    readme_summary = repo_index.get("readme_summary") or {}
    readme_path = readme_summary.get("path")
    readme_line_range = readme_summary.get("line_range")
    dependencies = repo_index.get("dependencies") or {}
    config_files = repo_index.get("config_files") or []

    evidence_catalog: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    def register_evidence(evidence: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not evidence or not validate_evidence(evidence):
            return None
        if not any(item["id"] == evidence["id"] for item in evidence_catalog):
            evidence_catalog.append(evidence)
        return evidence

    def evidence_has_line_range(evidence: Dict[str, Any]) -> bool:
        for source in evidence.get("sources") or []:
            line_range = source.get("line_range")
            if line_range and line_range.get("start") and line_range.get("end"):
                return True
        return False

    readme_evidence = None
    if readme_path and readme_line_range:
        readme_evidence = register_evidence(
            make_evidence(
                "readme",
                [
                    {
                        "kind": "readme",
                        "file": readme_path,
                        "line_range": readme_line_range,
                    }
                ],
                "readme_summary",
                "medium",
            )
        )

    tree_evidence = None
    if tree_entries:
        sources = []
        for entry in tree_entries[:8]:
            if isinstance(entry, str):
                sources.append({"kind": "structure", "file": entry.strip().rstrip("/")})
        if sources:
            tree_evidence = register_evidence(
                make_evidence("structure", sources, "repo_tree_entries", "medium")
            )

    dependency_sources: List[Dict[str, Any]] = []
    for item in dependencies.get("python") or []:
        if isinstance(item, dict) and item.get("name"):
            dependency_sources.append(
                {"kind": "dependency", "file": item.get("source"), "symbol": item.get("name")}
            )
    for item in dependencies.get("node") or []:
        if isinstance(item, dict) and item.get("name"):
            dependency_sources.append(
                {"kind": "dependency", "file": item.get("source"), "symbol": item.get("name")}
            )
    dependency_evidence = None
    if dependency_sources:
        dependency_evidence = register_evidence(
            make_evidence("dependency", dependency_sources, "dependency_files", "medium")
        )

    port_evidence = None
    if ports and (readme_evidence or config_files):
        sources = []
        if readme_evidence:
            sources.extend(readme_evidence.get("sources") or [])
        for name in config_files[:3]:
            sources.append({"kind": "config", "file": name})
        strength = "medium" if config_files else "weak"
        port_evidence = register_evidence(
            make_evidence("config", sources, "port_hints", strength)
        )

    primary_spotlight = (spotlights.get("items") or [{}])[0] if isinstance(spotlights, dict) else {}
    spotlight_evidence = register_evidence(primary_spotlight.get("evidence"))

    scenes: List[Dict[str, Any]] = []

    def add_scene(scene_id: str, duration: int, shots: List[Dict[str, Any]], reason: str) -> None:
        if not shots:
            skipped.append({"scene_id": scene_id, "reason": reason})
            return
        scenes.append({"id": scene_id, "duration": duration, "shots": shots})

    if readme_evidence:
        add_scene(
            "intro",
            3,
            [
                {
                    "t_start": 0,
                    "t_end": 3,
                    "type": "title",
                    "ref": {"kind": "repo", "name": repo_name},
                    "animations": ["fadeIn"],
                    "overlays": [{"type": "text", "text": f"{repo_name} Visual Pack"}],
                    "evidence_id": readme_evidence["id"],
                    "evidence_required": True,
                }
            ],
            "readme_evidence_missing",
        )
    else:
        skipped.append({"scene_id": "intro", "reason": "readme_evidence_missing"})

    if tree_evidence:
        add_scene(
            "tree",
            4,
            [
                {
                    "t_start": 0,
                    "t_end": 4,
                    "type": "tree",
                    "ref": {"kind": "tree", "entries": tree_entries[:8]},
                    "animations": ["slideUp"],
                    "overlays": [{"type": "text", "text": "Project Tree"}],
                    "evidence_id": tree_evidence["id"],
                    "evidence_required": True,
                }
            ],
            "tree_evidence_missing",
        )
    else:
        skipped.append({"scene_id": "tree", "reason": "tree_evidence_missing"})

    if dependency_evidence and dependency_evidence.get("type") in {"dependency", "call_graph"}:
        add_scene(
            "graph",
            4,
            [
                {
                    "t_start": 0,
                    "t_end": 4,
                    "type": "graph",
                    "ref": {"kind": "graph"},
                    "animations": ["fadeIn"],
                    "overlays": [{"type": "text", "text": "Repository Graph"}],
                    "evidence_id": dependency_evidence["id"],
                    "evidence_required": True,
                }
            ],
            "dependency_evidence_missing",
        )
    else:
        skipped.append({"scene_id": "graph", "reason": "dependency_evidence_missing"})

    if spotlight_evidence and evidence_has_line_range(spotlight_evidence):
        highlight_range = None
        if primary_spotlight.get("line_range"):
            highlight_range = primary_spotlight.get("line_range")
        elif primary_spotlight.get("highlights"):
            highlight_range = primary_spotlight["highlights"][0]
        else:
            highlight_range = {
                "start_line": primary_spotlight.get("start_line", 1),
                "end_line": primary_spotlight.get("end_line", 1),
            }
        add_scene(
            "code",
            5,
            [
                {
                    "t_start": 0,
                    "t_end": 5,
                    "type": "code",
                    "ref": {
                        "kind": "spotlight",
                        "index": 0,
                        "span": highlight_range,
                    },
                    "animations": ["focus"],
                    "overlays": [{"type": "text", "text": "Code Spotlight"}],
                    "evidence_id": spotlight_evidence["id"],
                    "evidence_required": True,
                }
            ],
            "spotlight_evidence_missing",
        )
    else:
        skipped.append({"scene_id": "code", "reason": "spotlight_evidence_missing"})

    if port_evidence and evidence_strength_rank(port_evidence.get("strength")) >= evidence_strength_rank("medium"):
        add_scene(
            "run",
            3,
            [
                {
                    "t_start": 0,
                    "t_end": 3,
                    "type": "summary",
                    "ref": {"kind": "ports", "items": ports},
                    "animations": ["fadeIn"],
                    "overlays": [{"type": "text", "text": "Run Hints"}],
                    "evidence_id": port_evidence["id"],
                    "evidence_required": True,
                }
            ],
            "port_evidence_missing",
        )
    else:
        skipped.append({"scene_id": "run", "reason": "port_evidence_missing"})

    total_duration = sum(scene.get("duration", 0) for scene in scenes)
    return {
        "generated_at": time.time(),
        "template_version": template_version or repo_index.get("template_version") or VISUAL_TEMPLATE_VERSION,
        "scenes": scenes,
        "total_duration": total_duration,
        "repo_graph_meta": repo_graph.get("meta") or {},
        "spotlight_count": len(spotlights.get("items") or []),
        "evidence_catalog": evidence_catalog,
        "meta": {"skipped_scenes": skipped},
    }
