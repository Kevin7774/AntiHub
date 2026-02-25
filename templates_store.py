import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from config import ROOT_DIR, TEMPLATES_PATH

DEFAULT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "template_id": "todo-list-app",
        "name": "Todo List App｜成功路径【Node/Compose/Dockerfile】",
        "group": "✅ 成功路径",
        "description": "Docker 官方 Node 示例，Dockerfile + Compose 完整。",
        "repo_url": "https://github.com/dockersamples/todo-list-app",
        "default_mode": "auto",
        "default_ref": "main",
        "suggested_env_keys": [],
        "dimensions": [
            "Node",
            "Dockerfile",
            "Compose"
        ],
        "expected": {
            "status": "RUNNING"
        },
        "what_to_verify": "构建成功并可访问（默认 3000），日志显示 Dockerfile 策略。"
    },
    {
        "template_id": "node-bulletin-board",
        "name": "Node Bulletin Board｜成功路径【子目录/Dockerfile/Node】",
        "group": "✅ 成功路径",
        "description": "Docker 官方示例，Dockerfile 位于子目录。",
        "repo_url": "https://github.com/dockersamples/node-bulletin-board",
        "default_mode": "auto",
        "default_ref": "master",
        "suggested_env_keys": [],
        "dimensions": [
            "子目录",
            "Dockerfile",
            "Node"
        ],
        "expected": {
            "status": "RUNNING"
        },
        "what_to_verify": "context_path 指向 bulletin-board-app 后可构建运行。",
        "context_path": "bulletin-board-app"
    },
    {
        "template_id": "uvicorn-poetry",
        "name": "Uvicorn Poetry｜成功路径【Python/FastAPI/Dockerfile】",
        "group": "✅ 成功路径",
        "description": "Python + FastAPI 示例镜像，包含 Dockerfile。",
        "repo_url": "https://github.com/max-pfeiffer/uvicorn-poetry",
        "default_mode": "auto",
        "default_ref": "main",
        "suggested_env_keys": [],
        "dimensions": [
            "Python",
            "FastAPI",
            "Dockerfile"
        ],
        "expected": {
            "status": "RUNNING"
        },
        "what_to_verify": "容器能启动 Uvicorn 服务，日志正常。"
    },
    {
        "template_id": "golang-sample-app",
        "name": "Golang Sample｜成功路径【Go/Dockerfile/多阶段】",
        "group": "✅ 成功路径",
        "description": "Go 示例应用，包含多种 Dockerfile。",
        "repo_url": "https://github.com/codefresh-contrib/golang-sample-app",
        "default_mode": "auto",
        "default_ref": "master",
        "suggested_env_keys": [],
        "dimensions": [
            "Go",
            "Dockerfile",
            "多阶段"
        ],
        "expected": {
            "status": "RUNNING"
        },
        "what_to_verify": "默认 Dockerfile 构建成功并监听 8080。",
        "dockerfile_path": "Dockerfile"
    },
    {
        "template_id": "docker-static-site",
        "name": "Static Site (Nginx)｜成功路径【静态站点/Nginx/Dockerfile】",
        "group": "✅ 成功路径",
        "description": "最小静态站点示例，Dockerfile 在根目录。",
        "repo_url": "https://github.com/nishanttotla/DockerStaticSite",
        "default_mode": "auto",
        "default_ref": "master",
        "suggested_env_keys": [],
        "dimensions": [
            "静态站点",
            "Nginx",
            "Dockerfile"
        ],
        "expected": {
            "status": "RUNNING"
        },
        "what_to_verify": "容器启动后可访问静态页面。"
    },
    {
        "template_id": "docker-static-website",
        "name": "Static Website｜成功路径【静态站点/Dockerfile】",
        "group": "✅ 成功路径",
        "description": "轻量静态站点示例，Dockerfile 在根目录。",
        "repo_url": "https://github.com/lipanski/docker-static-website",
        "default_mode": "auto",
        "default_ref": "master",
        "suggested_env_keys": [],
        "dimensions": [
            "静态站点",
            "Dockerfile",
            "轻量"
        ],
        "expected": {
            "status": "RUNNING"
        },
        "what_to_verify": "构建成功并可访问默认站点。"
    },
    {
        "template_id": "miniprogram-quickstart",
        "name": "WeChat MiniProgram Quickstart｜Showcase【小程序/说明书】",
        "group": "🎯 Showcase",
        "description": "官方小程序 quickstart，适合展示说明书与展示模式。",
        "repo_url": "https://github.com/wechat-miniprogram/miniprogram-quickstart",
        "default_mode": "showcase",
        "default_ref": "master",
        "suggested_env_keys": [],
        "dimensions": [
            "小程序",
            "无Dockerfile",
            "说明书"
        ],
        "expected": {
            "status": "SHOWCASE_READY"
        },
        "what_to_verify": "无需 Dockerfile，说明书生成完成且状态为 SHOWCASE_READY。"
    }
]

_CACHE_TTL_SECONDS = 30
_CACHE: Dict[str, Any] = {"loaded_at": 0.0, "templates": None}
_REMOTE_CACHE: Dict[str, Dict[str, Any]] = {}
_REMOTE_CACHE_TTL_SECONDS = 60


def _is_remote_repo(repo_url: str) -> bool:
    return "://" in repo_url or repo_url.startswith("git@")


def _parse_owner_from_repo_url(repo_url: str) -> Optional[str]:
    candidate = repo_url.strip()
    if not candidate:
        return None
    if candidate.startswith("git@github.com:"):
        path = candidate.split("git@github.com:", 1)[-1]
    elif "github.com/" in candidate:
        path = candidate.split("github.com/", 1)[-1]
    else:
        return None
    path = path.split("?", 1)[0].strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return parts[0]
    return None


def _infer_owner_from_git_config() -> Optional[str]:
    config_path = Path(ROOT_DIR) / ".git" / "config"
    if not config_path.exists():
        return None
    try:
        raw = config_path.read_text(encoding="utf-8")
    except Exception:
        return None
    current_remote = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("[remote "):
            if "origin" in line:
                current_remote = "origin"
            else:
                current_remote = None
        elif current_remote == "origin" and line.startswith("url"):
            _, value = line.split("=", 1)
            return _parse_owner_from_repo_url(value.strip())
    return None


def _infer_fixtures_repo_url() -> Optional[str]:
    overrides = [
        os.getenv("TEMPLATES_FIXTURES_REPO", "").strip(),
        os.getenv("TEMPLATES_FIXTURES_URL", "").strip(),
        os.getenv("TEMPLATE_REPO_URL", "").strip(),
    ]
    for override in overrides:
        if not override:
            continue
        if override.startswith("http://") or override.startswith("https://") or override.startswith("git@"):
            return override
        if "/" in override:
            return f"https://github.com/{override}"
    owner = None
    gh_repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if gh_repo and "/" in gh_repo:
        owner = gh_repo.split("/", 1)[0]
    if not owner:
        owner = _infer_owner_from_git_config()
    if owner:
        return f"https://github.com/{owner}/antihub-fixtures"
    return None


def _expand_repo_url(repo_url: str) -> str:
    if "<OWNER>" in repo_url or "<YourOrg>" in repo_url:
        override = _infer_fixtures_repo_url()
        if override:
            return override
    return repo_url


def _remote_check_enabled() -> bool:
    value = str(
        os.getenv("TEMPLATE_REMOTE_CHECK", os.getenv("TEMPLATES_REMOTE_CHECK", "true"))
    ).strip().lower()
    return value in {"1", "true", "yes"}


def _remote_check_strict() -> bool:
    value = str(
        os.getenv(
            "TEMPLATE_REMOTE_CHECK_STRICT",
            os.getenv("TEMPLATES_REMOTE_CHECK_STRICT", "false"),
        )
    ).strip().lower()
    return value in {"1", "true", "yes"}


def _remote_check_timeout() -> float:
    raw = str(
        os.getenv(
            "TEMPLATE_REMOTE_CHECK_TIMEOUT",
            os.getenv("TEMPLATES_REMOTE_CHECK_TIMEOUT", "2.0"),
        )
    ).strip()
    try:
        return float(raw)
    except ValueError:
        return 2.0


def _remote_repo_exists(repo_url: str) -> Optional[bool]:
    if not _remote_check_enabled():
        return None
    if "<" in repo_url or ">" in repo_url:
        return None
    cached = _REMOTE_CACHE.get(repo_url)
    now = time.time()
    if cached and now - float(cached.get("ts", 0.0)) < _REMOTE_CACHE_TTL_SECONDS:
        return cached.get("exists")
    if not (repo_url.startswith("http://") or repo_url.startswith("https://")):
        return None
    timeout = _remote_check_timeout()
    strict = _remote_check_strict()
    exists: Optional[bool] = None
    try:
        req = urllib.request.Request(repo_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or 200
        if 200 <= status < 400:
            exists = True
        elif status == 404:
            exists = False
        else:
            exists = False if strict else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            exists = False
        else:
            exists = False if strict else None
    except Exception:
        exists = False if strict else None
    _REMOTE_CACHE[repo_url] = {"exists": exists, "ts": now}
    return exists


def _load_from_path(path: Path) -> Optional[List[Dict[str, Any]]]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if path.suffix.lower() in {".yaml", ".yml"}:
        payload = yaml.safe_load(raw) or []
    else:
        payload = json.loads(raw or "[]")
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict)]


def load_templates(force: bool = False) -> List[Dict[str, Any]]:
    now = time.time()
    if not force and _CACHE["templates"] is not None:
        if now - float(_CACHE["loaded_at"] or 0.0) < _CACHE_TTL_SECONDS:
            return list(_CACHE["templates"])

    path = Path(TEMPLATES_PATH)
    templates = _load_from_path(path)
    if not templates:
        templates = DEFAULT_TEMPLATES

    normalized: List[Dict[str, Any]] = []
    for item in templates:
        template_id = str(item.get("template_id") or "").strip()
        name = str(item.get("name") or "").strip()
        repo_url = str(item.get("repo_url") or "").strip()
        if repo_url:
            repo_url = _expand_repo_url(repo_url)
        if not repo_url:
            continue
        if not _is_remote_repo(repo_url):
            candidate = Path(repo_url)
            if not candidate.is_absolute():
                candidate = Path(ROOT_DIR) / candidate
            if not candidate.exists():
                continue
            repo_url = str(candidate.resolve())
        else:
            exists = _remote_repo_exists(repo_url)
            if exists is False:
                continue
        if not template_id or not name or not repo_url:
            continue
        expected = item.get("expected")
        if not isinstance(expected, dict):
            expected = None
        what_to_verify = str(item.get("what_to_verify") or "").strip()
        normalized.append(
            {
                "template_id": template_id,
                "name": name,
                "group": str(item.get("group") or "").strip() or None,
                "description": str(item.get("description") or "").strip(),
                "repo_url": repo_url,
                "dockerfile_path": str(item.get("dockerfile_path") or "").strip() or None,
                "context_path": str(item.get("context_path") or "").strip() or None,
                "default_mode": str(item.get("default_mode") or "deploy").strip().lower(),
                "default_ref": str(item.get("default_ref") or item.get("ref") or "auto").strip(),
                "suggested_env_keys": [
                    str(key) for key in (item.get("suggested_env_keys") or []) if str(key).strip()
                ],
                "tags": [str(tag) for tag in (item.get("tags") or []) if str(tag).strip()],
                "dimensions": [
                    str(tag) for tag in (item.get("dimensions") or []) if str(tag).strip()
                ],
                "expected": expected,
                "what_to_verify": what_to_verify or None,
            }
        )

    _CACHE["loaded_at"] = now
    _CACHE["templates"] = normalized
    return list(normalized)


def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    template_id = str(template_id or "").strip()
    if not template_id:
        return None
    for item in load_templates():
        if item.get("template_id") == template_id:
            return dict(item)
    return None
