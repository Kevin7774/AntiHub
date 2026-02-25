import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from analyze.signals import sanitize_text
from config import VISUAL_GRAPH_MAX_NODES


def _normalize_dep_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _guess_node_type(label: str, is_dir: bool = False) -> str:
    lower = (label or "").lower()
    if "readme" in lower or lower.endswith((".md", ".rst", ".txt", ".adoc")):
        return "document"
    if any(
        marker in lower
        for marker in ("service", "api", "gateway", "worker", "server", "client", "handler", "controller")
    ):
        return "service"
    if any(marker in lower for marker in ("pipeline", "workflow", "flow", "task", "job")):
        return "process"
    if is_dir:
        return "module"
    if lower.endswith((".json", ".yaml", ".yml", ".toml", ".sql", ".db", ".sqlite")):
        return "data"
    if lower.endswith(
        (
            ".py",
            ".js",
            ".mjs",
            ".cjs",
            ".ts",
            ".tsx",
            ".jsx",
            ".go",
            ".rs",
            ".java",
            ".rb",
            ".php",
            ".cs",
            ".swift",
        )
    ):
        return "module"
    return "concept"


def _extract_import_symbols(snippet: str, language: str) -> List[str]:
    text = snippet or ""
    if not text:
        return []
    symbols: List[str] = []

    lower_lang = (language or "").lower()
    if "python" in lower_lang or ".py" in lower_lang or lower_lang == "py":
        for match in re.findall(r"^\s*from\s+([A-Za-z0-9_\.]+)\s+import\s+[A-Za-z0-9_*., ]+", text, re.MULTILINE):
            symbols.append(match)
        for line in re.findall(r"^\s*import\s+([A-Za-z0-9_., ]+)", text, re.MULTILINE):
            for part in str(line).split(","):
                token = part.strip().split(" as ")[0].strip()
                if token:
                    symbols.append(token)

    for match in re.findall(r"""import\s+(?:.+?\s+from\s+)?['"]([^'"]+)['"]""", text):
        symbols.append(match)
    for match in re.findall(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", text):
        symbols.append(match)

    seen: set[str] = set()
    ordered: List[str] = []
    for item in symbols:
        normalized = sanitize_text(str(item or "")).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _find_local_module_id(symbol: str, path_to_id: Dict[str, str]) -> Optional[str]:
    value = (symbol or "").strip()
    if not value or value.startswith("@"):
        return None
    token = value.strip("./")
    if not token:
        return None
    token_path = token.replace(".", "/")
    candidates = [
        token_path,
        f"{token_path}.py",
        f"{token_path}.ts",
        f"{token_path}.tsx",
        f"{token_path}.js",
        f"{token_path}.jsx",
        f"{token_path}/__init__.py",
        f"{token_path}/index.ts",
        f"{token_path}/index.tsx",
        f"{token_path}/index.js",
        f"{token_path}/index.jsx",
    ]
    for candidate in candidates:
        if candidate in path_to_id:
            return path_to_id[candidate]
    for path, node_id in path_to_id.items():
        if path.startswith(f"{token_path}/"):
            return node_id
    return None


def _layout_nodes(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> None:
    centers = {
        "concept": (0.0, -30.0),
        "module": (-320.0, 40.0),
        "service": (0.0, 260.0),
        "data": (320.0, 40.0),
        "document": (-320.0, -240.0),
        "process": (320.0, -240.0),
    }
    degree: Dict[str, int] = {str(node.get("id")): 0 for node in nodes}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        weight = int(edge.get("weight") or 1)
        if source in degree:
            degree[source] += weight
        if target in degree:
            degree[target] += weight

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        if node.get("id") == "repo":
            node["x"] = 0
            node["y"] = 0
            continue
        node_type = str(node.get("type") or "concept")
        grouped.setdefault(node_type, []).append(node)

    per_ring = 9
    for node_type, items in grouped.items():
        center_x, center_y = centers.get(node_type, centers["concept"])
        items.sort(
            key=lambda item: (
                -(degree.get(str(item.get("id")), 0)),
                -int(item.get("score") or 0),
                str(item.get("label") or ""),
            )
        )
        for index, node in enumerate(items):
            ring = index // per_ring
            slot = index % per_ring
            angle = ((math.pi * 2) / per_ring) * slot + ring * 0.2
            radius = 60 + ring * 62
            node["x"] = round(center_x + math.cos(angle) * radius, 2)
            node["y"] = round(center_y + math.sin(angle) * radius, 2)


def build_knowledge_graph(
    repo_index: Dict[str, Any],
    repo_graph: Dict[str, Any],
    spotlights: Dict[str, Any],
    max_nodes: Optional[int] = None,
) -> Dict[str, Any]:
    repo_name = str(repo_index.get("repo_name") or "Repository").strip() or "Repository"
    max_nodes = max_nodes or max(VISUAL_GRAPH_MAX_NODES, 96)

    nodes: List[Dict[str, Any]] = []
    node_ids: set[str] = set()
    edges_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    path_to_id: Dict[str, str] = {}
    dep_map: Dict[str, str] = {}
    truncated = False

    def add_node(
        node_id: str,
        label: str,
        node_type: str,
        *,
        note: Optional[str] = None,
        score: int = 10,
        path: Optional[str] = None,
    ) -> Optional[str]:
        nonlocal truncated
        if not node_id or node_id in node_ids:
            return node_id if node_id in node_ids else None
        if len(nodes) >= max_nodes:
            truncated = True
            return None
        payload: Dict[str, Any] = {
            "id": node_id,
            "label": sanitize_text(label).strip() or node_id,
            "type": node_type if node_type in {"module", "service", "data", "document", "process", "concept"} else "concept",
            "score": int(score),
            "x": 0,
            "y": 0,
        }
        if note:
            payload["note"] = sanitize_text(note).strip()
        nodes.append(payload)
        node_ids.add(node_id)
        if path:
            path_to_id[path] = node_id
        return node_id

    def add_edge(source: str, target: str, relation: str, weight: int = 1) -> None:
        if not source or not target or source == target:
            return
        if source not in node_ids or target not in node_ids:
            return
        key = (source, target, relation)
        if key in edges_by_key:
            edges_by_key[key]["weight"] += max(1, int(weight))
            return
        edges_by_key[key] = {
            "source": source,
            "target": target,
            "relation": sanitize_text(relation).strip() or "关联",
            "weight": max(1, int(weight)),
        }

    add_node("repo", repo_name, "concept", note="仓库根节点", score=100)

    tree_entries = ((repo_index.get("tree") or {}).get("entries") or [])[: max_nodes * 2]
    stack: List[Tuple[int, str, str]] = [(0, "", "repo")]
    for raw in tree_entries:
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
        parent_path = stack[-1][1] if stack else ""
        parent_id = stack[-1][2] if stack else "repo"
        node_path = f"{parent_path}/{name}" if parent_path else name
        node_id = f"path:{node_path}"
        added = add_node(
            node_id,
            name,
            _guess_node_type(node_path, is_dir=is_dir),
            note=f"层级 {depth}",
            score=max(6, 26 - depth * 2),
            path=node_path,
        )
        if not added:
            continue
        add_edge(parent_id, node_id, "包含", 1)
        if is_dir:
            stack.append((depth, node_path, node_id))

    dependencies = repo_index.get("dependencies") or {}
    for runtime in ("python", "node"):
        for item in (dependencies.get(runtime) or [])[:40]:
            if not isinstance(item, dict):
                continue
            dep_name = sanitize_text(str(item.get("name") or "")).strip()
            if not dep_name:
                continue
            dep_key = _normalize_dep_key(dep_name)
            if not dep_key:
                continue
            dep_id = f"dep:{dep_key}"
            added = add_node(dep_id, dep_name, "data", note=f"{runtime} 依赖", score=12)
            if not added:
                continue
            dep_map[dep_key] = dep_id
            add_edge("repo", dep_id, "依赖", 1)

    entrypoints = repo_index.get("entrypoints") or []
    for item in entrypoints[:12]:
        if not isinstance(item, dict):
            continue
        entry_path = sanitize_text(str(item.get("path") or "")).strip()
        if not entry_path:
            continue
        entry_id = path_to_id.get(entry_path)
        if not entry_id:
            label = Path(entry_path).name if "/" in entry_path else entry_path
            entry_id = add_node(
                f"entry:{entry_path}",
                label,
                _guess_node_type(entry_path, is_dir=False),
                note="检测到入口文件",
                score=24,
                path=entry_path,
            )
        if entry_id:
            add_edge("repo", entry_id, "入口", 2)

    spotlight_items = (spotlights.get("items") or []) if isinstance(spotlights, dict) else []
    for item in spotlight_items[:14]:
        if not isinstance(item, dict):
            continue
        file_path = sanitize_text(str(item.get("file_path") or "")).strip()
        if not file_path:
            continue
        file_id = path_to_id.get(file_path)
        if not file_id:
            file_id = add_node(
                f"file:{file_path}",
                Path(file_path).name or file_path,
                _guess_node_type(file_path, is_dir=False),
                note="来自代码片段",
                score=20,
                path=file_path,
            )
            if file_id:
                add_edge("repo", file_id, "包含", 1)
        if not file_id:
            continue

        language = str(item.get("language") or "")
        snippet = sanitize_text(str(item.get("snippet") or ""))
        imports = _extract_import_symbols(snippet, language)
        for symbol in imports[:10]:
            local_id = _find_local_module_id(symbol, path_to_id)
            if local_id:
                add_edge(file_id, local_id, "调用", 1)
                continue
            dep_key = _normalize_dep_key(symbol.split("/")[0].split(".")[0])
            dep_id = dep_map.get(dep_key)
            if dep_id:
                add_edge(file_id, dep_id, "依赖", 1)
                continue
            token = sanitize_text(symbol.split("/")[-1]).strip()
            if not token:
                continue
            ext_key = _normalize_dep_key(token)
            if not ext_key:
                continue
            ext_id = add_node(
                f"ext:{ext_key}",
                token,
                "service",
                note="外部模块引用",
                score=8,
            )
            if ext_id:
                add_edge(file_id, ext_id, "调用", 1)

    config_files = repo_index.get("config_files") or []
    if config_files:
        process_id = add_node("process:runtime", "运行配置", "process", note="部署与运行相关配置", score=18)
        for config_name in config_files[:8]:
            path_value = sanitize_text(str(config_name or "")).strip()
            if not path_value:
                continue
            config_id = path_to_id.get(path_value)
            if not config_id:
                config_id = add_node(
                    f"config:{path_value}",
                    Path(path_value).name or path_value,
                    _guess_node_type(path_value, is_dir=False),
                    note="配置文件",
                    score=12,
                    path=path_value,
                )
                if config_id:
                    add_edge("repo", config_id, "包含", 1)
            if config_id and process_id:
                add_edge(config_id, process_id, "定义", 1)

    edges = [{"id": f"edge-{index}", **item} for index, item in enumerate(edges_by_key.values(), start=1)]
    edges.sort(key=lambda item: (-int(item.get("weight") or 1), str(item.get("relation") or "")))
    _layout_nodes(nodes, edges)

    readme_summary_text = str((repo_index.get("readme_summary") or {}).get("text") or "")
    relation_kinds = len({str(edge.get("relation") or "") for edge in edges})
    top_entities = sorted(
        nodes,
        key=lambda item: (-int(item.get("score") or 0), str(item.get("label") or "")),
    )
    focus_modules = [str(item.get("label") or "") for item in top_entities if item.get("id") != "repo"][:6]
    python_dep_count = len((dependencies.get("python") or []))
    node_dep_count = len((dependencies.get("node") or []))
    entry_count = len([item for item in entrypoints if isinstance(item, dict) and item.get("path")])

    risk_signals: List[str] = []
    if entry_count == 0:
        risk_signals.append("未识别明确入口文件，启动路径需要人工确认。")
    if not readme_summary_text:
        risk_signals.append("README 摘要为空，业务语义证据较弱。")
    if len(spotlight_items) == 0:
        risk_signals.append("缺少代码片段证据，模块调用关系主要来自结构推断。")
    if truncated:
        risk_signals.append("图谱节点达到上限，已按优先级截断展示。")

    key_findings: List[str] = [
        f"共识别 {len(nodes)} 个实体、{len(edges)} 条关系，关系类型 {relation_kinds} 种。",
        f"依赖分布为 Python {python_dep_count} 项、Node {node_dep_count} 项。",
    ]
    if focus_modules:
        key_findings.append(f"核心关注实体：{'、'.join(focus_modules[:4])}。")
    if entry_count:
        key_findings.append(f"识别到 {entry_count} 个入口候选，可作为阅读与验证起点。")

    suggestions: List[str] = []
    if focus_modules:
        suggestions.append(f"优先从 {focus_modules[0]} 开始，沿“调用/依赖”边逐层验证关键路径。")
    suggestions.append("先核对入口文件与配置文件的连线，再判断运行链路是否闭合。")
    if python_dep_count + node_dep_count > 0:
        suggestions.append("对高频依赖节点做版本与替代方案评估，降低后续接入风险。")

    return {
        "generated_at": time.time(),
        "template_version": repo_index.get("template_version"),
        "nodes": nodes,
        "edges": edges,
        "analysis": {
            "summary": f"{repo_name} 的知识图谱已完成自动构建，可直接用于结构理解与关系校验。",
            "key_findings": key_findings,
            "risk_signals": risk_signals,
            "suggestions": suggestions,
            "focus_modules": focus_modules,
        },
        "meta": {
            "source": "repo_index+repo_graph+spotlights",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sentence_count": len(spotlight_items),
            "source_length": len(readme_summary_text),
            "truncated": truncated,
            "max_nodes": max_nodes,
            "repo_graph_nodes": len((repo_graph or {}).get("nodes") or []),
        },
    }
