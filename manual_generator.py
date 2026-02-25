import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import MANUAL_GENERATOR_VERSION, MANUAL_MAX_README_CHARS, MANUAL_TREE_DEPTH

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

CONFIG_FILE_PATTERNS = {
    ".env",
    ".env.example",
    "docker-compose.yml",
    "docker-compose.yaml",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
}
CONFIG_SUFFIXES = (".yaml", ".yml", ".toml", ".json")

TEMPLATE_BASELINE = """
说明书
一句话简介
功能概览
快速开始
一键部署
本地运行
Docker 运行
配置与环境变量
目录结构解读
架构/流程图
常见问题与注意事项
版本与生成信息
"""

BADGE_PATTERNS = (
    re.compile(r"^!\[.*\]\(.*\)$"),
    re.compile(r"\[!\[.*\]\(.*\)\]\(.*\)$"),
    re.compile(r"shields\.io", re.IGNORECASE),
)

ENV_LINE_PATTERN = re.compile(r"^\s*([A-Z][A-Z0-9_]{1,})\s*[:=]")
ENV_INLINE_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b\s*=")
PORT_PATTERN = re.compile(r"(?:port|端口)\D{0,10}(\d{2,5})", re.IGNORECASE)

KEY_PATH_HINTS = {
    "app.json": "小程序全局配置",
    "project.config.json": "小程序工程配置",
    "pages": "页面目录",
    "components": "组件目录",
    "miniprogram": "小程序源码目录",
    "src": "核心源码目录",
    "api": "API 接口层",
    "backend": "后端服务代码",
    "frontend": "前端应用代码",
    "server": "服务端入口目录",
    "main.py": "Python 入口脚本",
    "app.py": "应用入口脚本",
    "server.py": "服务端入口脚本",
    "index.js": "前端入口脚本",
    "main.js": "前端入口脚本",
    "Dockerfile": "容器构建入口",
    "package.json": "前端依赖与脚本",
    "requirements.txt": "Python 依赖清单",
    "pyproject.toml": "Python 项目配置",
}


def generate_manual(
    repo_path: Path,
    env_keys: List[str],
    repo_name: Optional[str] = None,
    tree_depth: Optional[int] = None,
    readme_max_chars: Optional[int] = None,
    previous_manual: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    start = time.time()
    repo_name = repo_name or repo_path.name
    tree_depth = tree_depth if tree_depth is not None else MANUAL_TREE_DEPTH
    readme_max_chars = readme_max_chars if readme_max_chars is not None else MANUAL_MAX_README_CHARS

    readme_path, readme_text = _read_readme(repo_path, readme_max_chars)
    readme_info = _parse_readme(readme_text)
    dockerfile_path = repo_path / "Dockerfile"
    has_dockerfile = dockerfile_path.exists()
    docker_info = _parse_dockerfile(dockerfile_path) if has_dockerfile else {}
    tree_output, file_count = _build_tree(repo_path, tree_depth)
    config_files = _find_config_files(repo_path, tree_depth)
    package_info = _parse_package_json(repo_path / "package.json")
    python_info = _parse_python_signals(repo_path)

    key_paths = _describe_key_paths(repo_path)
    repo_kind = _detect_repo_kind(repo_path, package_info, python_info)

    env_candidates = _collect_env_keys(
        repo_path,
        env_keys,
        docker_info,
        readme_text,
    )
    port_hint = (
        docker_info.get("expose")
        or _extract_port_from_readme(readme_text)
        or "-"
    )

    one_liner = _build_one_liner(repo_name, readme_info, repo_kind, docker_info, python_info, package_info)
    features = _build_features(readme_info, repo_kind, docker_info, package_info, python_info, key_paths)
    local_run = _infer_local_run(repo_path, repo_kind, package_info, python_info)
    docker_run = _infer_docker_run(repo_name, docker_info)
    mermaid = _build_mermaid(repo_kind, key_paths, docker_info, python_info, package_info)
    faq_items = _build_faq(repo_kind, docker_info, package_info, python_info, readme_path)

    manual_markdown = _render_manual(
        repo_name=repo_name,
        one_liner=one_liner,
        features=features,
        local_run=local_run,
        docker_run=docker_run,
        env_keys=env_candidates,
        port_hint=port_hint,
        config_files=config_files,
        tree_output=tree_output,
        key_paths=key_paths,
        docker_info=docker_info,
        readme_path=readme_path,
        mermaid=mermaid,
        faq_items=faq_items,
    )

    similarity_score = _compute_similarity(manual_markdown, previous_manual)
    warnings: List[str] = []
    if similarity_score is not None and similarity_score > 0.75:
        warnings.append("MANUAL_TOO_GENERIC")

    fingerprint_source = "|".join(sorted(_collect_repo_fingerprint(repo_path)))
    fingerprint_source += f"|{readme_info.get('title','')}|{readme_info.get('summary','')}"
    repo_fingerprint = hashlib.sha1(fingerprint_source.encode("utf-8", errors="ignore")).hexdigest()[:10]

    time_cost_ms = int((time.time() - start) * 1000)
    meta = {
        "generated_at": time.time(),
        "generator_version": MANUAL_GENERATOR_VERSION,
        "repo_fingerprint": repo_fingerprint,
        "similarity_score": similarity_score,
        "warnings": warnings,
        "signals": {
            "repo_kind": repo_kind,
            "readme_title": readme_info.get("title"),
            "readme_summary": readme_info.get("summary"),
            "has_readme": bool(readme_text),
            "has_dockerfile": has_dockerfile,
            "docker_info": docker_info,
            "config_files": config_files,
            "package_scripts": package_info.get("scripts") if package_info else [],
            "python_entrypoints": python_info.get("entrypoints") if python_info else [],
            "env_candidates": env_candidates,
            "key_paths": key_paths,
            "tree_depth": tree_depth,
            "file_count": file_count,
        },
        "time_cost_ms": time_cost_ms,
    }

    manual_markdown = (
        manual_markdown.replace("{generated_at}", str(meta["generated_at"]))
        .replace("{generator_version}", str(meta["generator_version"]))
        .replace("{signals}", json.dumps(meta["signals"], ensure_ascii=False))
        .replace("{time_cost_ms}", str(meta["time_cost_ms"]))
        .replace("{repo_fingerprint}", repo_fingerprint)
        .replace("{similarity_score}", str(similarity_score))
        .replace("{warnings}", ", ".join(warnings) or "None")
    )
    return manual_markdown, meta


def _read_readme(repo_path: Path, max_chars: int) -> Tuple[Optional[str], str]:
    for name in README_CANDIDATES:
        path = repo_path / name
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            return str(path.name), text[:max_chars]
    return None, ""


def _parse_readme(readme_text: str) -> Dict[str, Any]:
    cleaned: List[str] = []
    for raw in readme_text.splitlines():
        line = raw.strip()
        if not line:
            cleaned.append("")
            continue
        if _is_badge_line(line):
            continue
        cleaned.append(line)

    title = ""
    headings: List[str] = []
    bullets: List[str] = []
    for line in cleaned:
        if line.startswith("#") and not title:
            title = line.lstrip("#").strip()
            continue
        if line.startswith("## ") or line.startswith("### "):
            heading = line.lstrip("#").strip()
            if heading and heading not in headings:
                headings.append(heading)
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if item and item not in bullets:
                bullets.append(item)
        if re.match(r"\d+\.\s+", line):
            item = re.sub(r"^\d+\.\s+", "", line).strip()
            if item and item not in bullets:
                bullets.append(item)

    summary = ""
    paragraphs = _split_paragraphs(cleaned)
    for para in paragraphs:
        if para.startswith("#"):
            continue
        if para.startswith(("- ", "* ")):
            continue
        summary = para
        break
    summary = _trim_summary(summary)

    env_keys = _extract_env_keys_from_text(readme_text)
    return {
        "title": title,
        "summary": summary,
        "headings": headings,
        "bullets": bullets,
        "env_keys": env_keys,
    }


def _split_paragraphs(lines: List[str]) -> List[str]:
    paragraphs: List[str] = []
    buffer: List[str] = []
    for line in lines:
        if not line:
            if buffer:
                paragraphs.append(" ".join(buffer).strip())
                buffer = []
            continue
        buffer.append(line)
    if buffer:
        paragraphs.append(" ".join(buffer).strip())
    return paragraphs


def _trim_summary(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= 80:
        return text
    return text[:78].rstrip() + "…"


def _is_badge_line(line: str) -> bool:
    if line.startswith("![") or line.startswith("[!["):
        return True
    return any(pattern.search(line) for pattern in BADGE_PATTERNS)


def _parse_package_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    scripts = []
    deps = []
    if isinstance(data, dict):
        raw_scripts = data.get("scripts")
        if isinstance(raw_scripts, dict):
            scripts = list(raw_scripts.keys())
        raw_deps = data.get("dependencies")
        raw_dev = data.get("devDependencies")
        for raw in (raw_deps, raw_dev):
            if isinstance(raw, dict):
                deps.extend(list(raw.keys()))
    return {
        "scripts": scripts,
        "dependencies": sorted(set(deps)),
    }


def _parse_python_signals(repo_path: Path) -> Dict[str, Any]:
    deps: List[str] = []
    entrypoints: List[str] = []
    frameworks: List[str] = []

    req_path = repo_path / "requirements.txt"
    if req_path.exists():
        deps.extend(_parse_requirements(req_path))

    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        deps.extend(_parse_pyproject(pyproject))

    for filename in ["main.py", "app.py", "server.py", "wsgi.py", "manage.py"]:
        path = repo_path / filename
        if path.exists():
            entrypoints.append(filename)
            framework = _detect_framework(path)
            if framework and framework not in frameworks:
                frameworks.append(framework)

    return {
        "dependencies": sorted(set(deps)),
        "entrypoints": entrypoints,
        "frameworks": frameworks,
    }


def _parse_requirements(path: Path) -> List[str]:
    deps: List[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = re.split(r"[=<>]", line)[0].strip()
            if name:
                deps.append(name)
    except Exception:
        return []
    return deps


def _parse_pyproject(path: Path) -> List[str]:
    deps: List[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    for line in text.splitlines():
        if "dependencies" in line and "=" in line:
            continue
        match = re.search(r"\"([A-Za-z0-9_.-]+)\"", line)
        if match:
            deps.append(match.group(1))
    return deps


def _detect_framework(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if "FastAPI(" in text:
        return "FastAPI"
    if "Flask(" in text:
        return "Flask"
    if "django" in text.lower():
        return "Django"
    return None


def _detect_repo_kind(repo_path: Path, package_info: Dict[str, Any], python_info: Dict[str, Any]) -> str:
    if (repo_path / "app.json").exists() or (repo_path / "project.config.json").exists():
        return "miniapp"
    if package_info.get("scripts"):
        return "node"
    if python_info.get("dependencies") or python_info.get("entrypoints"):
        return "python"
    if (repo_path / "Dockerfile").exists():
        return "docker"
    return "unknown"


def _build_one_liner(
    repo_name: str,
    readme_info: Dict[str, Any],
    repo_kind: str,
    docker_info: Dict[str, Any],
    python_info: Dict[str, Any],
    package_info: Dict[str, Any],
) -> str:
    title = readme_info.get("title") or ""
    summary = readme_info.get("summary") or ""

    keywords = []
    if repo_kind == "miniapp":
        keywords.append("微信小程序")
    if "FastAPI" in python_info.get("frameworks", []):
        keywords.append("FastAPI")
    if "Flask" in python_info.get("frameworks", []):
        keywords.append("Flask")
    deps = set(package_info.get("dependencies", []))
    if "react" in deps:
        keywords.append("React")
    if "vue" in deps:
        keywords.append("Vue")
    if docker_info.get("base"):
        keywords.append("Docker")

    keyword_text = " / ".join(keywords) if keywords else "仓库"

    if title and summary:
        return f"{title}：{summary}"
    if title:
        return f"{title}（{keyword_text}）"

    entrypoints = python_info.get("entrypoints") or []
    entry = entrypoints[0] if entrypoints else "入口未显式标注"
    return f"{repo_name}：基于 {keyword_text}，入口 {entry}，支持容器化运行。"


def _build_features(
    readme_info: Dict[str, Any],
    repo_kind: str,
    docker_info: Dict[str, Any],
    package_info: Dict[str, Any],
    python_info: Dict[str, Any],
    key_paths: List[Tuple[str, str]],
) -> List[str]:
    features: List[str] = []
    seen: set[str] = set()

    def add(feature: str, evidence: str) -> None:
        text = feature.strip()
        if not text or text in seen:
            return
        seen.add(text)
        features.append(f"- [ ] {text} (evidence: {evidence})")

    for item in readme_info.get("bullets", [])[:3]:
        add(item, "README")

    for heading in readme_info.get("headings", [])[:3]:
        add(f"包含模块/章节：{heading}", "README")

    if repo_kind == "miniapp":
        add("小程序全局配置与页面路由", "app.json/pages")
        add("小程序组件化页面结构", "components/")
        add("支持微信开发者工具本地预览", "project.config.json")

    scripts = package_info.get("scripts", [])
    if scripts:
        if "dev" in scripts:
            add("本地开发脚本 npm run dev", "package.json scripts")
        if "build" in scripts:
            add("构建脚本 npm run build", "package.json scripts")
        if "start" in scripts:
            add("启动脚本 npm start", "package.json scripts")
        if "test" in scripts:
            add("测试脚本 npm run test", "package.json scripts")

    deps = set(package_info.get("dependencies", []))
    if "react" in deps:
        add("React 前端渲染", "package.json dependencies")
    if "vue" in deps:
        add("Vue 前端应用", "package.json dependencies")

    py_deps = set(python_info.get("dependencies", []))
    if "fastapi" in {d.lower() for d in py_deps}:
        add("FastAPI API 服务", "requirements.txt/pyproject.toml")
    if "flask" in {d.lower() for d in py_deps}:
        add("Flask Web 服务", "requirements.txt/pyproject.toml")
    if "django" in {d.lower() for d in py_deps}:
        add("Django 应用结构", "requirements.txt/pyproject.toml")

    if docker_info.get("expose"):
        add(f"容器暴露端口 {docker_info['expose']}", "Dockerfile EXPOSE")
    if docker_info.get("cmd"):
        add("容器启动命令已定义", "Dockerfile CMD")

    # Fallback: use key paths to reach 5+ features
    for path, desc in key_paths:
        if len(features) >= 7:
            break
        add(f"包含关键路径 {path}（{desc}）", path)

    if len(features) < 5:
        add("项目结构已整理，可直接导入查看", "目录结构")
    return features[:10]


def _infer_local_run(
    repo_path: Path,
    repo_kind: str,
    package_info: Dict[str, Any],
    python_info: Dict[str, Any],
) -> List[str]:
    commands: List[str] = []

    if repo_kind == "miniapp":
        commands.append("使用微信开发者工具导入项目目录")
        if (repo_path / "project.config.json").exists():
            commands.append("保持 project.config.json 中的 appid 配置")
        commands.append("在工具内点击编译/预览")
        return commands

    scripts = package_info.get("scripts", [])
    if scripts:
        commands.append("依赖：Node.js 18+ / npm")
        commands.append("npm install")
        if "dev" in scripts:
            commands.append("npm run dev")
        elif "start" in scripts:
            commands.append("npm start")
        elif "serve" in scripts:
            commands.append("npm run serve")
        else:
            commands.append("npm run <script>")
        return commands

    if python_info.get("dependencies") or python_info.get("entrypoints"):
        commands.append("依赖：Python 3.9+")
        if (repo_path / "requirements.txt").exists():
            commands.append("pip install -r requirements.txt")
        elif (repo_path / "pyproject.toml").exists():
            commands.append("pip install -U pip && pip install -e .")

        entrypoints = python_info.get("entrypoints", [])
        if "manage.py" in entrypoints:
            commands.append("python manage.py runserver 0.0.0.0:8000")
            return commands
        for entry in ["main.py", "app.py", "server.py"]:
            if entry in entrypoints:
                framework = _detect_framework(repo_path / entry)
                module_name = entry.replace(".py", "")
                if framework == "FastAPI":
                    commands.append(f"uvicorn {module_name}:app --host 0.0.0.0 --port 8000")
                elif framework == "Flask":
                    commands.append(f"flask --app {module_name} run --host 0.0.0.0 --port 8000")
                else:
                    commands.append(f"python {entry}")
                return commands
        commands.append("python main.py")
        return commands

    commands.append("依赖：按 README 指引准备")
    commands.append("启动命令：请参考 README 或 Dockerfile")
    return commands


def _infer_docker_run(repo_name: str, docker_info: Dict[str, Any]) -> List[str]:
    expose = docker_info.get("expose") or "8080"
    tag = repo_name.lower().replace(" ", "-")
    return [
        f"docker build -t {tag}:local .",
        f"docker run --rm -p 8080:{expose} {tag}:local",
    ]


def _parse_dockerfile(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "base": None,
        "expose": None,
        "cmd": None,
        "entrypoint": None,
        "env_keys": [],
        "arg_keys": [],
    }
    if not path.exists():
        return info
    try:
        contents = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return info
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("FROM "):
            info["base"] = line.split(" ", 1)[1].strip()
        if upper.startswith("EXPOSE "):
            match = re.search(r"EXPOSE\s+(\d+)", line, re.IGNORECASE)
            if match:
                info["expose"] = match.group(1)
        if upper.startswith("CMD "):
            info["cmd"] = line
        if upper.startswith("ENTRYPOINT "):
            info["entrypoint"] = line
        if upper.startswith("ENV "):
            keys = _extract_env_keys_from_line(line.replace("ENV ", "", 1))
            info["env_keys"].extend(keys)
        if upper.startswith("ARG "):
            keys = _extract_env_keys_from_line(line.replace("ARG ", "", 1))
            info["arg_keys"].extend(keys)
    info["env_keys"] = sorted(set(info["env_keys"]))
    info["arg_keys"] = sorted(set(info["arg_keys"]))
    return info


def _build_tree(repo_path: Path, depth: int) -> Tuple[str, int]:
    lines: List[str] = []
    file_count = 0

    def walk(current: Path, prefix: str, current_depth: int) -> None:
        nonlocal file_count
        if current_depth > depth:
            return
        entries = [p for p in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))]
        entries = [p for p in entries if p.name not in IGNORED_DIRS]
        for idx, entry in enumerate(entries):
            connector = "└──" if idx == len(entries) - 1 else "├──"
            name = f"{entry.name}/" if entry.is_dir() else entry.name
            lines.append(f"{prefix}{connector} {name}")
            if entry.is_dir():
                next_prefix = prefix + ("    " if idx == len(entries) - 1 else "│   ")
                walk(entry, next_prefix, current_depth + 1)
            else:
                file_count += 1

    lines.append(f"{repo_path.name}/")
    try:
        walk(repo_path, "", 1)
    except Exception:
        lines.append("(目录解析失败)")

    return "\n".join(lines), file_count


def _find_config_files(repo_path: Path, depth: int) -> List[str]:
    config_files: List[str] = []
    for root, dirs, files in os.walk(repo_path):
        rel = Path(root).relative_to(repo_path)
        if any(part in IGNORED_DIRS for part in rel.parts):
            dirs[:] = []
            continue
        if len(rel.parts) > depth:
            dirs[:] = []
            continue
        for file in files:
            name = file
            lower = name.lower()
            if lower in CONFIG_FILE_PATTERNS or lower.endswith(CONFIG_SUFFIXES):
                path = str((Path(root) / file).relative_to(repo_path))
                if path not in config_files:
                    config_files.append(path)
    return sorted(config_files)


def _collect_env_keys(
    repo_path: Path,
    env_keys: List[str],
    docker_info: Dict[str, Any],
    readme_text: str,
) -> List[str]:
    keys: List[str] = []
    keys.extend([k for k in env_keys if k])

    for filename in [".env.example", ".env"]:
        path = repo_path / filename
        if path.exists():
            keys.extend(_extract_env_keys_from_file(path))

    keys.extend(readme_text and _extract_env_keys_from_text(readme_text) or [])
    keys.extend(docker_info.get("env_keys", []))
    keys.extend(docker_info.get("arg_keys", []))

    keys = [k for k in keys if k]
    return sorted(set(keys))


def _extract_env_keys_from_file(path: Path) -> List[str]:
    keys: List[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = ENV_LINE_PATTERN.match(line)
            if match:
                keys.append(match.group(1))
    except Exception:
        return []
    return keys


def _extract_env_keys_from_text(text: str) -> List[str]:
    keys = ENV_INLINE_PATTERN.findall(text)
    return sorted(set(keys))


def _extract_env_keys_from_line(line: str) -> List[str]:
    keys: List[str] = []
    for token in line.split():
        if "=" in token:
            token = token.split("=", 1)[0]
        if token.isupper() and token.replace("_", "").isalnum():
            keys.append(token)
    return keys


def _extract_port_from_readme(text: str) -> Optional[str]:
    match = PORT_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def _describe_key_paths(repo_path: Path) -> List[Tuple[str, str]]:
    key_paths: List[Tuple[str, str]] = []
    seen: set[str] = set()

    def add(path: Path, desc: str) -> None:
        rel = str(path.relative_to(repo_path))
        if rel in seen:
            return
        seen.add(rel)
        key_paths.append((rel + ("/" if path.is_dir() else ""), desc))

    for name, desc in KEY_PATH_HINTS.items():
        path = repo_path / name
        if path.exists():
            add(path, desc)

    for name in ["pages", "components", "api", "backend", "frontend", "src", "server"]:
        path = repo_path / name
        if path.exists() and path.is_dir():
            add(path, KEY_PATH_HINTS.get(name, "关键目录"))

    if len(key_paths) < 5:
        for entry in sorted(repo_path.iterdir(), key=lambda p: p.name.lower()):
            if entry.name in IGNORED_DIRS:
                continue
            add(entry, "顶层目录/文件")
            if len(key_paths) >= 8:
                break

    return key_paths[:10]


def _build_mermaid(
    repo_kind: str,
    key_paths: List[Tuple[str, str]],
    docker_info: Dict[str, Any],
    python_info: Dict[str, Any],
    package_info: Dict[str, Any],
) -> str:
    paths = [path for path, _ in key_paths]
    has_pages = any(path.startswith("pages/") for path in paths)
    has_components = any(path.startswith("components/") for path in paths)
    has_api = any(path.startswith("api/") or path.startswith("backend/") for path in paths)
    has_frontend = any(path.startswith("frontend/") or path.startswith("src/") for path in paths)

    if repo_kind == "miniapp":
        nodes = [
            "U[用户]",
            "M[小程序(app.json)]",
        ]
        if has_pages:
            nodes.append("P[pages/]")
        if has_components:
            nodes.append("C[components/]")
        if any(path.startswith("project.config.json") for path in paths):
            nodes.append("CFG[project.config.json]")
        edges = ["U --> M"]
        if has_pages:
            edges.append("M --> P")
        if has_components:
            edges.append("P --> C" if has_pages else "M --> C")
        if has_api:
            edges.append("P --> API[backend/api]" if has_pages else "M --> API[backend/api]")
        return "\n".join(["flowchart TD"] + [f"  {n}" for n in nodes] + [f"  {e}" for e in edges])

    entry_label = "服务入口"
    for candidate in ["app.py", "main.py", "server.py", "index.js", "main.js", "index.html"]:
        if any(path.startswith(candidate) for path in paths):
            entry_label = candidate
            break
    nodes = ["U[用户]", f"S[{entry_label}]"]
    edges = ["U --> S"]

    if has_frontend:
        nodes.append("UI[src/frontend]")
        edges = ["U --> UI", "UI --> S"]

    deps = set(package_info.get("dependencies", []))
    py_deps = set(python_info.get("dependencies", []))
    if any(dep in {"redis", "mysql", "postgres", "postgresql"} for dep in deps | {d.lower() for d in py_deps}):
        nodes.append("DB[数据存储]")
        edges.append("S --> DB")

    if docker_info.get("expose"):
        nodes.append(f"P[端口 {docker_info['expose']}] ")
        edges.append("S --> P")

    return "\n".join(["flowchart TD"] + [f"  {n}" for n in nodes] + [f"  {e}" for e in edges])


def _build_faq(
    repo_kind: str,
    docker_info: Dict[str, Any],
    package_info: Dict[str, Any],
    python_info: Dict[str, Any],
    readme_path: Optional[str],
) -> List[str]:
    items: List[str] = []
    if not readme_path:
        items.append("README 缺失：建议补充功能说明与启动步骤。")
    if repo_kind == "miniapp":
        items.append("小程序需使用微信开发者工具导入并配置 appid。")
    if not docker_info.get("expose"):
        items.append("未检测到 EXPOSE 端口，请确认应用监听端口。")
    if not package_info.get("scripts") and not python_info.get("entrypoints"):
        items.append("未识别到启动脚本，请在 README 中注明运行方式。")
    if docker_info.get("cmd"):
        items.append("Dockerfile CMD 已定义，建议与本地启动命令保持一致。")
    while len(items) < 3:
        items.append("如遇启动失败，请查看构建日志与依赖安装步骤。")
    return items[:5]


def _collect_repo_fingerprint(repo_path: Path) -> List[str]:
    paths: List[str] = []
    for root, dirs, files in os.walk(repo_path):
        rel = Path(root).relative_to(repo_path)
        if any(part in IGNORED_DIRS for part in rel.parts):
            dirs[:] = []
            continue
        if len(rel.parts) > 3:
            dirs[:] = []
            continue
        for file in files:
            paths.append(str(rel / file))
    return paths


def _compute_similarity(current: str, previous: Optional[str]) -> Optional[float]:
    baseline = previous or TEMPLATE_BASELINE
    current_tokens = set(re.findall(r"\w+", current.lower()))
    prev_tokens = set(re.findall(r"\w+", baseline.lower()))
    if not current_tokens or not prev_tokens:
        return None
    intersection = current_tokens & prev_tokens
    union = current_tokens | prev_tokens
    return round(len(intersection) / max(1, len(union)), 3)


def _render_manual(
    repo_name: str,
    one_liner: str,
    features: List[str],
    local_run: List[str],
    docker_run: List[str],
    env_keys: List[str],
    port_hint: str,
    config_files: List[str],
    tree_output: str,
    key_paths: List[Tuple[str, str]],
    docker_info: Dict[str, Any],
    readme_path: Optional[str],
    mermaid: str,
    faq_items: List[str],
) -> str:
    feature_lines = "\n".join(features)
    local_lines = "\n".join([f"- {item}" for item in local_run])
    docker_lines = "\n".join([f"- {item}" for item in docker_run])
    if env_keys:
        env_lines = "\n".join([f"  - {item}" for item in env_keys])
        env_note = ""
    else:
        env_lines = "  - (无)"
        env_note = "- 未检测到显式环境变量示例，建议补充 .env.example"
    config_lines = "\n".join([f"  - {item}" for item in config_files]) or "  - (无)"

    docker_notes = []
    if docker_info.get("base"):
        docker_notes.append(f"Dockerfile 基础镜像：{docker_info['base']}")
    if docker_info.get("cmd"):
        docker_notes.append(f"Dockerfile CMD：{docker_info['cmd']}")
    if docker_info.get("entrypoint"):
        docker_notes.append(f"Dockerfile ENTRYPOINT：{docker_info['entrypoint']}")
    docker_note_block = "\n".join(f"- {item}" for item in docker_notes) or "- (未解析到 Dockerfile 关键指令)"
    readme_note = readme_path or "README 缺失"

    key_path_lines = "\n".join([f"- {path}: {desc}" for path, desc in key_paths])
    faq_lines = "\n".join([f"- {item}" for item in faq_items])

    return f"""# {repo_name} 说明书
## 1. 一句话简介
- {one_liner}

## 2. 功能概览
{feature_lines}

## 3. 快速开始
### 3.1 一键部署（本平台）
- 创建 Case：在控制台填写 repo/ref/env_keys，提交后获取 case_id
- 访问地址：RUNNING 后使用 access_url 打开

### 3.2 本地运行（如可推断）
{local_lines}

### 3.3 Docker 运行（如可推断）
{docker_lines}

## 4. 配置与环境变量（只展示 keys）
- 端口：{port_hint}
- 环境变量：
{env_lines}
{env_note}
- 关键配置文件：
{config_lines}

## 5. 目录结构解读
```text
{tree_output}
```
关键目录/文件说明：
{key_path_lines}

Dockerfile/README 说明：
{docker_note_block}
- README: {readme_note}

## 6. 架构/流程图（Mermaid）
```mermaid
{mermaid}
```

## 7. 常见问题与注意事项
{faq_lines}

## 8. 版本与生成信息
- generated_at: {{generated_at}}
- generator_version: {{generator_version}}
- repo_fingerprint: {{repo_fingerprint}}
- similarity_score: {{similarity_score}}
- warnings: {{warnings}}
- signals: {{signals}}
- time_cost_ms: {{time_cost_ms}}
"""
