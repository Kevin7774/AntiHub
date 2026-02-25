import json
import logging
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import docker
import redis
import yaml
from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_failure

_LOGGER = logging.getLogger(__name__)

from analyze.llm import LLMError
from analyze.report_store import repo_cache_key
from analyze.service import AnalysisFailure, run_analysis
from config import (
    ANALYZE_TIMEOUT_SECONDS,
    BUILD_ROOT,
    CASE_LABEL_KEY,
    CASE_LABEL_MANAGED,
    CELERY_ALWAYS_EAGER,
    DOCKER_BUILD_NETWORK,
    DOCKER_BUILD_NO_CACHE,
    DOCKER_BUILD_TIMEOUT_SECONDS,
    DOCKER_BUILDKIT_DEFAULT,
    DOCKERFILE_SEARCH_DEPTH,
    GIT_ENABLE_LFS,
    GIT_ENABLE_SUBMODULES,
    MANUAL_MAX_README_CHARS,
    MANUAL_ROOT,
    MANUAL_TREE_DEPTH,
    PORT_MODE,
    PORT_POOL_END,
    PORT_POOL_START,
    PUBLIC_HOST,
    REDIS_DISABLED,
    REDIS_URL,
    REPO_SCAN_DEPTH,
    STARTUP_TIMEOUT_SECONDS,
    TIMEOUT_MANUAL_SECONDS,
    VISUALIZE_TIMEOUT_SECONDS,
    WORKER_DEAD_LETTER_KEY,
    WORKER_TASK_MAX_RETRIES,
    WORKER_TASK_RETRY_DELAY_SECONDS,
    get_network_settings,
    get_proxy_config,
)
from docker_ops import (
    build_image,
    build_proxy_args,
    build_proxy_env,
    cleanup_build_dir,
    detect_exposed_port,
    run_container_dynamic,
    run_container_fixed,
    wait_for_container_running,
)
from dockerfile_discovery import DockerfileAmbiguousError, resolve_dockerfile
from dockerfile_parser import parse_dockerfile_from
from errors import ERROR_CODE_MAP
from git_ops import GitRefNotFoundError, clone_repo, has_lfs, normalize_ref
from manual_generator import generate_manual
from storage import (
    append_log,
    get_case,
    get_manual,
    get_manual_status,
    record_manual_stats,
    release_analyze_lock,
    release_visualize_lock,
    set_manual,
    set_manual_status,
    update_case,
)
from strategy_engine import (
    generate_dockerfile,
    inspect_repo,
    select_strategy,
)
from visualize.service import VisualFailure, run_visualize
from visualize.store import visual_cache_key

_USE_REDIS = not (REDIS_DISABLED or CELERY_ALWAYS_EAGER)
_BROKER_URL = REDIS_URL if _USE_REDIS else "memory://"
_BACKEND_URL = REDIS_URL if _USE_REDIS else "cache+memory://"

celery_app = Celery("agent_platform", broker=_BROKER_URL, backend=_BACKEND_URL)
if not _USE_REDIS:
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_ignore_result = True
celery_app.conf.task_default_retry_delay = WORKER_TASK_RETRY_DELAY_SECONDS
celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1


class _MemoryRedis:
    def __init__(self) -> None:
        self._store: Dict[str, Tuple[str, float | None]] = {}
        self._lists: Dict[str, List[str]] = {}

    def _purge(self) -> None:
        now = time.time()
        expired = [key for key, (_, ttl) in self._store.items() if ttl is not None and ttl <= now]
        for key in expired:
            self._store.pop(key, None)

    def set(self, key: str, value: str, nx: bool = False, ex: Optional[int] = None) -> bool:
        self._purge()
        if nx and key in self._store:
            return False
        expiry = time.time() + ex if ex else None
        self._store[key] = (value, expiry)
        return True

    def get(self, key: str) -> Optional[str]:
        self._purge()
        item = self._store.get(key)
        if not item:
            return None
        return item[0]

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def rpush(self, key: str, value: str) -> int:
        items = self._lists.setdefault(key, [])
        items.append(str(value))
        return len(items)

    def lrange(self, key: str, start: int, end: int) -> List[str]:
        items = list(self._lists.get(key, []))
        if end == -1:
            end = len(items) - 1
        return items[start : end + 1]


redis_client: Any = redis.Redis.from_url(REDIS_URL, decode_responses=True) if _USE_REDIS else _MemoryRedis()

PORT_LOCK_PREFIX = "port_lock:"


def _enqueue_dead_letter(
    *,
    task_name: str,
    task_id: Optional[str],
    args: Any,
    kwargs: Any,
    exception: Exception,
    retries: int,
    max_retries: int,
) -> None:
    payload = {
        "task": str(task_name or "unknown"),
        "task_id": str(task_id or ""),
        "args": args,
        "kwargs": kwargs,
        "retries": int(retries),
        "max_retries": int(max_retries),
        "error_type": type(exception).__name__,
        "error_message": str(exception),
        "ts": time.time(),
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    try:
        redis_client.rpush(WORKER_DEAD_LETTER_KEY, serialized)
    except Exception:
        _LOGGER.exception("failed to enqueue dead letter")
    _LOGGER.error("task moved to dead letter: %s", serialized)


@task_failure.connect  # type: ignore[misc]
def _handle_task_failure(  # noqa: ANN001
    sender=None,
    task_id=None,
    exception=None,
    args=None,
    kwargs=None,
    einfo=None,  # noqa: ARG001
    **_extras,
) -> None:
    if sender is None or exception is None:
        return
    retries = int(getattr(getattr(sender, "request", None), "retries", 0) or 0)
    sender_max = getattr(sender, "max_retries", None)
    max_retries = WORKER_TASK_MAX_RETRIES if sender_max in {None, -1} else int(sender_max)
    if retries < max_retries:
        return
    _enqueue_dead_letter(
        task_name=str(getattr(sender, "name", "unknown")),
        task_id=str(task_id or ""),
        args=args,
        kwargs=kwargs,
        exception=exception if isinstance(exception, Exception) else RuntimeError(str(exception)),
        retries=retries,
        max_retries=max_retries,
    )


class BuildError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _now_ts() -> float:
    return time.time()


def _is_network_error(message: str) -> bool:
    lowered = message.lower()
    patterns = [
        "temporary failure in name resolution",
        "no such host",
        "could not resolve",
        "lookup",
        "network is unreachable",
        "i/o timeout",
        "connection timed out",
        "dial tcp",
        "dns",
    ]
    return any(token in lowered for token in patterns)


def _is_dns_error(message: str) -> bool:
    lowered = message.lower()
    patterns = [
        "temporary failure in name resolution",
        "no such host",
        "could not resolve",
        "name or service not known",
        "server misbehaving",
        "lookup",
    ]
    return any(token in lowered for token in patterns)


def _is_registry_error(message: str) -> bool:
    lowered = message.lower()
    patterns = [
        "registry-1.docker.io",
        "index.docker.io",
        "docker.io",
        "service unavailable",
        "connection refused",
        "connection reset by peer",
        "tls handshake timeout",
        "x509",
    ]
    return any(token in lowered for token in patterns)


def _truncate_list(values: List[str], limit: int = 20) -> List[str]:
    if not values:
        return []
    if len(values) <= limit:
        return values
    return values[:limit] + [f"...+{len(values) - limit} more"]


def _sanitize_proxy_host(proxy_value: str) -> Optional[str]:
    value = (proxy_value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if not parsed.scheme:
        parsed = urlparse(f"http://{value}")
    host = parsed.hostname
    if not host:
        return None
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host


def _proxy_is_reachable(proxy_value: str, timeout: float = 0.4) -> bool:
    value = (proxy_value or "").strip()
    if not value:
        return False
    parsed = urlparse(value)
    if not parsed.scheme:
        parsed = urlparse(f"http://{value}")
    host = parsed.hostname
    if not host:
        return False
    if host == "localhost" or host.startswith("127.") or host == "::1":
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_config_reachable(proxy_config: Dict[str, str]) -> bool:
    if not proxy_config:
        return False
    for key in ("http_proxy", "https_proxy"):
        if _proxy_is_reachable(proxy_config.get(key) or ""):
            return True
    return False


def _detect_docker_daemon_proxy(client: docker.DockerClient) -> Dict[str, str]:
    try:
        info = client.info()
    except Exception:
        return {}
    if not isinstance(info, dict):
        return {}
    http_proxy = str(info.get("HTTPProxy") or info.get("http_proxy") or "").strip()
    https_proxy = str(info.get("HTTPSProxy") or info.get("https_proxy") or "").strip()
    no_proxy = str(info.get("NoProxy") or info.get("no_proxy") or "").strip()
    return {
        "http_proxy": http_proxy,
        "https_proxy": https_proxy,
        "no_proxy": no_proxy,
    }


def _format_dockerfile_discovery_meta(
    meta: Dict[str, object],
    context_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, object]:
    scanned = list(meta.get("scanned_candidates") or [])
    primary = list(meta.get("primary_candidates") or [])
    backup = list(meta.get("backup_candidates") or [])
    ignored = list(meta.get("ignored_backups") or [])
    selected = meta.get("selected_dockerfile")
    selected_context = meta.get("selected_context_path")
    if not selected_context and context_dir and repo_root:
        try:
            selected_context = str(context_dir.relative_to(repo_root)) or "."
        except Exception:
            selected_context = None
    log_meta: Dict[str, object] = {
        "scanned_candidates": _truncate_list(scanned),
        "primary_candidates": _truncate_list(primary),
        "backup_candidates": _truncate_list(backup),
        "ignored_backups": _truncate_list(ignored),
        "non_unique_primary": bool(meta.get("non_unique_primary")),
        "selected_dockerfile": selected,
        "selected_backup": bool(meta.get("selected_backup")),
        "selection_reason": meta.get("selection_reason"),
        "dockerfile_candidates": _truncate_list(scanned),
        "backup_candidates_filtered": _truncate_list(ignored),
        "selected_dockerfile_path": selected,
        "selected_context_path": selected_context,
    }
    if meta.get("ambiguous_candidates") is not None:
        log_meta["ambiguous_candidates"] = _truncate_list(
            list(meta.get("ambiguous_candidates") or [])
        )
    if meta.get("how_to_fix"):
        log_meta["how_to_fix"] = meta.get("how_to_fix")
    return log_meta


def _log_dockerfile_discovery(
    case_id: str,
    meta: Optional[Dict[str, object]],
    context_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> None:
    if not meta:
        return
    log_meta = _format_dockerfile_discovery_meta(meta, context_dir, repo_root)
    publish_log(case_id, "system", f"[preflight] dockerfile_discovery={json.dumps(log_meta)}")
    _LOGGER.info("[preflight] dockerfile_discovery=%s", json.dumps(log_meta))


def _classify_network_error_code(message: str) -> Optional[str]:
    if _is_dns_error(message):
        return "DNS_RESOLUTION_FAILED"
    if _is_registry_error(message):
        return "REGISTRY_UNREACHABLE"
    if _is_network_error(message):
        return "DOCKER_BUILD_NETWORK_FAILED"
    return None


def _is_port_in_use_error(message: str) -> bool:
    lowered = message.lower()
    patterns = [
        "port is already allocated",
        "address already in use",
        "bind for 0.0.0.0",
    ]
    return any(token in lowered for token in patterns)


def _is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _ensure_ports_available(case_id: str, ports: List[int]) -> None:
    for port in ports:
        if not _is_port_available(port):
            publish_log(case_id, "system", f"[compose] port {port} already in use", level="ERROR")
            raise BuildError(f"PORT_IN_USE({port})", "PORT_IN_USE")


def _extract_compose_ports(compose_path: Path) -> List[int]:
    try:
        payload = yaml.safe_load(compose_path.read_text(encoding="utf-8", errors="replace")) or {}
    except Exception:
        return []
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, dict):
        return []
    ports: List[int] = []
    for service in services.values():
        if not isinstance(service, dict):
            continue
        raw_ports = service.get("ports") or []
        if not isinstance(raw_ports, list):
            continue
        for entry in raw_ports:
            host_port: Optional[int] = None
            if isinstance(entry, int):
                host_port = entry
            elif isinstance(entry, dict):
                published = entry.get("published") or entry.get("host_port")
                if isinstance(published, int):
                    host_port = published
                elif isinstance(published, str) and published.isdigit():
                    host_port = int(published)
            elif isinstance(entry, str):
                cleaned = entry.split("/")[0]
                parts = cleaned.split(":")
                if len(parts) == 3:
                    host = parts[1]
                elif len(parts) == 2:
                    host = parts[0]
                else:
                    host = parts[0]
                if host.isdigit():
                    host_port = int(host)
            if host_port is not None:
                ports.append(host_port)
    return sorted(set(ports))


def _select_compose_file(
    repo_root: Path,
    compose_file: Optional[str],
    repo_url: Optional[str],
) -> Tuple[Path, str]:
    if compose_file:
        candidate = repo_root / compose_file
        if candidate.exists():
            return candidate, "compose_file_specified"
    if repo_url and "frappe_docker" in repo_url and (repo_root / "pwd.yml").exists():
        return repo_root / "pwd.yml", "frappe_pwd_default"
    for name in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]:
        candidate = repo_root / name
        if candidate.exists():
            return candidate, "compose_standard_name"
    for candidate in sorted(repo_root.iterdir(), key=lambda p: p.name):
        if candidate.is_file() and candidate.suffix in {".yml", ".yaml"}:
            return candidate, "compose_any_yaml"
    raise BuildError("Compose file not found", "DOCKERFILE_NOT_FOUND")


def _run_compose_command(
    case_id: str,
    repo_root: Path,
    compose_path: Path,
    project_name: str,
    args: List[str],
    stream: str = "run",
) -> None:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = project_name
    cmd = ["docker", "compose", "-f", str(compose_path)] + args
    publish_log(case_id, "system", f"[compose] exec: {' '.join(cmd)}")
    try:
        with subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        ) as proc:
            if proc.stdout:
                for raw in proc.stdout:
                    line = raw.rstrip("\n")
                    if line:
                        publish_log(case_id, stream, line)
            exit_code = proc.wait()
    except FileNotFoundError as exc:
        raise BuildError("docker compose not available", "COMPOSE_NOT_AVAILABLE") from exc
    if exit_code != 0:
        raise BuildError(f"docker compose failed with exit code {exit_code}", "COMPOSE_UP_FAILED")


def _stream_compose_logs(
    case_id: str,
    repo_root: Path,
    compose_path: Path,
    project_name: str,
) -> None:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = project_name
    cmd = ["docker", "compose", "-f", str(compose_path), "logs", "-f", "--no-color"]
    try:
        with subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        ) as proc:
            if proc.stdout:
                for raw in proc.stdout:
                    line = raw.rstrip("\n")
                    if line:
                        publish_log(case_id, "runtime", line)
            proc.wait()
    except FileNotFoundError:
        publish_log(case_id, "system", "docker compose logs not available", level="ERROR")


def _wait_for_service_container(
    client: docker.DockerClient,
    project_name: str,
    service_name: str,
    timeout_seconds: int,
) -> Optional[docker.models.containers.Container]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"com.docker.compose.project={project_name}",
                    f"com.docker.compose.service={service_name}",
                ]
            },
        )
        if containers:
            return containers[0]
        time.sleep(2)
    return None


def _stream_service_logs(case_id: str, container: docker.models.containers.Container) -> None:
    try:
        for raw in container.logs(stream=True, follow=True, tail=50):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line:
                publish_log(case_id, "runtime", line)
    except Exception:
        return


def _wait_for_service_exit(
    container: docker.models.containers.Container,
    timeout_seconds: int,
) -> int:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        container.reload()
        state = container.attrs.get("State", {}) or {}
        status = state.get("Status")
        if status in {"exited", "dead"}:
            return int(state.get("ExitCode") or 0)
        time.sleep(2)
    raise TimeoutError("Service did not finish within timeout")


def _wait_for_port(host: str, port: int, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError:
            time.sleep(2)
    raise TimeoutError(f"Port {port} not ready")


def _wait_for_ping(host: str, port: int, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    url = f"http://{host}:{port}/api/method/ping"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if resp.status == 200 and "\"pong\"" in body:
                    return
        except urllib.error.URLError:
            time.sleep(2)
            continue
    raise TimeoutError("Healthcheck ping failed")


def _run_compose_case(
    case_id: str,
    repo_root: Path,
    compose_file: Optional[str],
    repo_url: Optional[str],
) -> Dict[str, Any]:
    compose_path, reason = _select_compose_file(repo_root, compose_file, repo_url)
    project_name = f"antihub-{case_id}"
    publish_log(case_id, "system", f"[compose] file={compose_path.name} reason={reason}")
    ports = _extract_compose_ports(compose_path)
    if not ports and compose_path.name == "pwd.yml":
        ports = [8080]
    if ports:
        _ensure_ports_available(case_id, ports)

    log_thread = Thread(
        target=_stream_compose_logs,
        args=(case_id, repo_root, compose_path, project_name),
        daemon=True,
    )
    log_thread.start()

    try:
        _run_compose_command(case_id, repo_root, compose_path, project_name, ["up", "-d"], stream="run")

        # wait for create-site if present
        try:
            payload = yaml.safe_load(compose_path.read_text(encoding="utf-8", errors="replace")) or {}
        except Exception:
            payload = {}
        services = payload.get("services") if isinstance(payload, dict) else {}
        service_names = sorted(services.keys()) if isinstance(services, dict) else []
        if isinstance(services, dict) and "create-site" in services:
            client = docker.from_env()
            container = _wait_for_service_container(client, project_name, "create-site", 900)
            if container:
                publish_log(case_id, "system", "[compose] create-site detected; streaming logs")
                _stream_service_logs(case_id, container)
                exit_code = _wait_for_service_exit(container, 600)
                if exit_code != 0:
                    raise BuildError("create-site exited with non-zero code", "COMPOSE_CONTAINER_EXITED")
                publish_log(case_id, "system", "[compose] create-site completed")
            else:
                publish_log(case_id, "system", "[compose] create-site container not found", level="WARNING")

        host_port = 8080 if 8080 in ports else (ports[0] if ports else 8080)
        publish_log(case_id, "system", f"[compose] waiting for port {host_port}")
        _wait_for_port("127.0.0.1", host_port, STARTUP_TIMEOUT_SECONDS)
        publish_log(case_id, "system", "[compose] port ready, checking ping")
        _wait_for_ping("127.0.0.1", host_port, STARTUP_TIMEOUT_SECONDS)
        publish_log(case_id, "system", "[compose] healthcheck ping ok")
    except Exception:
        _run_compose_command(
            case_id,
            repo_root,
            compose_path,
            project_name,
            ["down", "--remove-orphans", "--volumes"],
            stream="run",
        )
        raise

    access_url = f"http://{PUBLIC_HOST}:{host_port}"
    runtime = {
        "container_id": None,
        "host_port": host_port,
        "access_url": access_url,
        "started_at": time.time(),
        "exited_at": None,
        "exit_code": None,
        "ports": ports,
        "services": service_names,
    }
    return {
        "compose_project_name": project_name,
        "compose_file": str(compose_path.relative_to(repo_root)),
        "host_port": host_port,
        "access_url": access_url,
        "runtime": runtime,
    }


def _format_error_message(code: str, detail: str) -> str:
    info = ERROR_CODE_MAP.get(code) or {}
    base = str(info.get("message") or "").strip()
    hint = str(info.get("hint") or "").strip()
    summary = " ".join(part for part in [base, hint] if part)
    detail = detail.strip()
    if summary:
        if detail and detail not in summary:
            return f"{summary} (detail: {detail})"
        return summary
    return detail or "Unknown failure"


def _resolve_image_vars(image: str, build_args: Dict[str, str]) -> Tuple[str, bool]:
    pattern = re.compile(r"\$(\{)?([A-Za-z_][A-Za-z0-9_]*)\}?")
    unresolved = False

    def repl(match: re.Match) -> str:
        nonlocal unresolved
        key = match.group(2)
        if key in build_args:
            return build_args[key]
        unresolved = True
        return match.group(0)

    resolved = pattern.sub(repl, image)
    return resolved, unresolved


def _resolve_buildkit_setting(requested: Optional[bool]) -> bool:
    if requested is not None:
        return requested
    env_flag = os.getenv("DOCKER_BUILDKIT", "").strip().lower()
    if env_flag in {"1", "true", "yes"}:
        return True
    if env_flag in {"0", "false", "no"}:
        return False
    return DOCKER_BUILDKIT_DEFAULT


def _normalize_run_mode(raw_mode: Optional[str], auto_mode: bool) -> str:
    candidate = (raw_mode or "").strip().lower()
    if candidate == "deploy":
        candidate = "container"
    if not candidate:
        candidate = "auto" if auto_mode else "auto"
    if candidate not in {"auto", "container", "showcase", "compose"}:
        candidate = "auto"
    return candidate


def _plan_base_images(
    images: List[str],
    build_args: Dict[str, str],
    arg_defaults: Optional[Dict[str, str]] = None,
) -> tuple[List[str], List[str]]:
    resolved: List[str] = []
    warnings: List[str] = []
    merged_args: Dict[str, str] = {}
    if arg_defaults:
        merged_args.update(arg_defaults)
    merged_args.update(build_args)
    for image in images:
        resolved_image, unresolved = _resolve_image_vars(image, merged_args)
        if unresolved:
            warnings.append(f"UNRESOLVED_BASE_IMAGE:{image}")
            continue
        resolved.append(resolved_image)
    return resolved, warnings


def _pull_base_images(
    client: docker.DockerClient,
    images: List[str],
    case_id: Optional[str] = None,
) -> List[str]:
    pulled: List[str] = []
    for image in images:
        if case_id:
            publish_log(case_id, "system", f"[pull] Pulling base image {image}")
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                client.images.pull(image)
                last_error = None
                break
            except docker.errors.APIError as exc:
                last_error = exc
                if case_id:
                    publish_log(
                        case_id,
                        "system",
                        f"[pull] Failed to pull {image} (attempt {attempt}/3)",
                        level="ERROR",
                    )
                time.sleep(2 ** (attempt - 1))
        if last_error is not None:
            raise BuildError(
                f"Failed to pull base image {image}. Check registry mirror / network / DNS.",
                "DOCKER_BASE_IMAGE_PULL_FAILED",
            ) from last_error
        pulled.append(image)
    return pulled


def publish_log(case_id: str, stream: str, line: str, level: str = "INFO") -> None:
    payload = {"ts": _now_ts(), "stream": stream, "level": level, "line": line}
    append_log(case_id, payload)


def log_event(case_id: str, event: dict) -> None:
    if "stream" in event:
        line = event["stream"].rstrip()
        if line:
            publish_log(case_id, "build", line)
    elif "error" in event:
        publish_log(case_id, "build", event["error"].rstrip(), level="ERROR")
    else:
        publish_log(case_id, "build", json.dumps(event))


def reserve_port(case_id: str, exclude: Optional[set[int]] = None) -> int:
    exclude = exclude or set()
    for port in range(PORT_POOL_START, PORT_POOL_END + 1):
        if port in exclude:
            continue
        lock_key = f"{PORT_LOCK_PREFIX}{port}"
        if redis_client.set(lock_key, case_id, nx=True, ex=86400):
            return port
    raise BuildError("No available ports in pool", "PORT_POOL_EXHAUSTED")


def reserve_specific_port(case_id: str, port: int) -> None:
    lock_key = f"{PORT_LOCK_PREFIX}{port}"
    existing = redis_client.get(lock_key)
    if existing and existing != case_id:
        raise BuildError("Port already reserved", "PORT_IN_USE")
    redis_client.set(lock_key, case_id, ex=86400)


def release_port(port: Optional[int]) -> None:
    if port is None:
        return
    redis_client.delete(f"{PORT_LOCK_PREFIX}{port}")


def classify_error(exc: Exception) -> tuple[str, str]:
    message = str(exc)
    if isinstance(exc, BuildError):
        return exc.code, message
    if isinstance(exc, DockerfileAmbiguousError):
        return "DOCKERFILE_AMBIGUOUS", message
    if isinstance(exc, GitRefNotFoundError):
        return "GIT_REF_NOT_FOUND", message
    if isinstance(exc, SoftTimeLimitExceeded):
        return "TIMEOUT_BUILD", "Build task exceeded time limit"
    if isinstance(exc, subprocess.CalledProcessError):
        return "GIT_CLONE_FAILED", message
    if isinstance(exc, subprocess.TimeoutExpired):
        return "TIMEOUT_CLONE", message
    if isinstance(exc, docker.errors.BuildError):
        network_code = _classify_network_error_code(message)
        if network_code:
            return network_code, message
        return "DOCKER_BUILD_FAILED", message
    if isinstance(exc, docker.errors.APIError):
        if _is_port_in_use_error(message):
            return "PORT_IN_USE", message
        network_code = _classify_network_error_code(message)
        if network_code:
            return network_code, message
        return "DOCKER_API_ERROR", message
    if isinstance(exc, FileNotFoundError) and "Dockerfile" in message:
        return "DOCKERFILE_NOT_FOUND", message
    if "Container exited" in message:
        return "CONTAINER_EXITED", (
            "Container exited during startup. Ensure the process stays in the foreground."
        )
    if isinstance(exc, TimeoutError) or "timed out" in message.lower():
        return "TIMEOUT_RUN", (
            "Container startup timed out. Verify the app listens on 0.0.0.0 and the port matches EXPOSE."
        )
    return "UNEXPECTED_ERROR", message


def classify_manual_error(exc: Exception) -> tuple[str, str]:
    message = str(exc)
    if isinstance(exc, SoftTimeLimitExceeded):
        return "TIMEOUT_MANUAL", "Manual generation exceeded time limit"
    if isinstance(exc, GitRefNotFoundError):
        return "GIT_REF_NOT_FOUND", message
    if isinstance(exc, subprocess.CalledProcessError):
        return "MANUAL_CLONE_FAILED", message
    if isinstance(exc, subprocess.TimeoutExpired):
        return "TIMEOUT_CLONE", message
    return "MANUAL_GENERATION_FAILED", message


def stream_container_logs(case_id: str, container_id: str) -> None:
    client = docker.from_env()
    container = client.containers.get(container_id)
    try:
        for raw in container.logs(stream=True, follow=True, stdout=True, stderr=True):
            line = raw.decode(errors="ignore").rstrip()
            if line:
                publish_log(case_id, "run", line)
    finally:
        container.reload()
        exit_code = container.attrs.get("State", {}).get("ExitCode")
        finished_at = _now_ts()
        existing = get_case(case_id) or {}
        runtime = existing.get("runtime") or {}
        host_port = runtime.get("host_port") or existing.get("host_port")
        if exit_code is None or exit_code == 0:
            update_case(
                case_id,
                {
                    "status": "FINISHED",
                    "stage": "run",
                    "runtime": {
                        **runtime,
                        "exited_at": finished_at,
                        "exit_code": exit_code,
                    },
                },
            )
            publish_log(case_id, "system", f"Container exited with code {exit_code}")
        else:
            update_case(
                case_id,
                {
                    "status": "FAILED",
                    "stage": "run",
                    "error_code": "CONTAINER_EXIT_NONZERO",
                    "error_message": f"Container exited with code {exit_code}",
                    "runtime": {
                        **runtime,
                        "exited_at": finished_at,
                        "exit_code": exit_code,
                    },
                },
            )
            publish_log(case_id, "system", f"Container exited with code {exit_code}", level="ERROR")
        if PORT_MODE != "dynamic":
            release_port(host_port)


def launch_log_stream(case_id: str, container_id: str) -> None:
    t = Thread(target=stream_container_logs, args=(case_id, container_id), daemon=True)
    t.start()


def _queue_manual_if_needed(case_id: str, auto_manual: bool) -> None:
    if not auto_manual:
        return
    existing = get_case(case_id) or {}
    status_data = get_manual_status(case_id) or {}
    status = (status_data.get("status") or existing.get("manual_status") or "").upper()
    if status in {"PENDING", "RUNNING", "SUCCESS"}:
        return
    update_case(
        case_id,
        {
            "manual_status": "PENDING",
            "manual_error_code": None,
            "manual_error_message": None,
        },
    )
    set_manual_status(case_id, "PENDING")
    generate_manual_task.delay(case_id)


@celery_app.task(
    name="build_and_run",
    time_limit=DOCKER_BUILD_TIMEOUT_SECONDS,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": WORKER_TASK_MAX_RETRIES},
)
def build_and_run(
    case_id: str,
    repo_url: str,
    ref: Optional[str] = None,
    container_port: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    auto_analyze: bool = False,
    dockerfile_path: Optional[str] = None,
    compose_file: Optional[str] = None,
    context_path: Optional[str] = None,
    docker_build_network: Optional[str] = None,
    docker_no_cache: Optional[bool] = None,
    docker_build_args: Optional[Dict[str, str]] = None,
    enable_submodules: Optional[bool] = None,
    enable_lfs: Optional[bool] = None,
    mode: Optional[str] = None,
    auto_mode: bool = False,
    auto_manual: bool = True,
    build_use_buildkit: Optional[bool] = None,
) -> dict:
    ref = normalize_ref(ref)
    env = env or {}
    docker_build_args = docker_build_args or {}
    proxy_config = get_proxy_config()
    network_settings = get_network_settings()
    proxy_warning = None
    if proxy_config and network_settings.get("probe_proxy", True):
        reachable = _proxy_config_reachable(proxy_config)
        if not reachable:
            if network_settings.get("force_proxy", False):
                publish_log(
                    case_id,
                    "system",
                    "[preflight] proxy_probe=unreachable (force_proxy=true)",
                )
            else:
                publish_log(
                    case_id,
                    "system",
                    "[preflight] proxy_probe=unreachable; skip proxy injection",
                )
                _LOGGER.warning("Proxy unreachable; skipping proxy injection for build/run.")
                proxy_config = {}
                proxy_warning = "PROXY_UNREACHABLE"
    proxy_build_args = build_proxy_args(proxy_config)
    proxy_env = build_proxy_env(proxy_config)
    if proxy_build_args:
        for key, value in proxy_build_args.items():
            docker_build_args.setdefault(key, value)
    runtime_proxy_injected = False
    if network_settings.get("inject_runtime_proxy", True) and proxy_env:
        for key, value in proxy_env.items():
            if key not in env:
                env[key] = value
                runtime_proxy_injected = True
    docker_build_network = docker_build_network or DOCKER_BUILD_NETWORK
    docker_no_cache = DOCKER_BUILD_NO_CACHE if docker_no_cache is None else docker_no_cache
    enable_submodules = GIT_ENABLE_SUBMODULES if enable_submodules is None else enable_submodules
    enable_lfs = GIT_ENABLE_LFS if enable_lfs is None else enable_lfs
    run_mode = _normalize_run_mode(mode, auto_mode)
    buildkit_enabled = _resolve_buildkit_setting(build_use_buildkit)
    update_case(case_id, {"run_mode": run_mode})
    effective_network = docker_build_network
    network_warning = None
    if buildkit_enabled and docker_build_network == "bridge":
        effective_network = "default"
        network_warning = "BUILDKIT_NETWORK_BRIDGE_UNSUPPORTED: using default"
    if not compose_file and repo_url and "frappe_docker" in repo_url:
        compose_file = "pwd.yml"
    update_case(case_id, {"status": "CLONING", "stage": "clone", "compose_file": compose_file})
    target_dir = Path(BUILD_ROOT) / case_id
    host_port: Optional[int] = None
    port_reserved = False
    container_started = False
    cleanup_build_dir_on_exit = True
    try:
        publish_log(case_id, "system", f"[clone] Cloning repo {repo_url} (ref {ref or 'auto'})...")
        if enable_submodules:
            publish_log(case_id, "system", "[clone] Submodule support enabled")
        if enable_lfs:
            publish_log(case_id, "system", "[clone] LFS support enabled")
        clone_result = clone_repo(
            repo_url,
            ref,
            target_dir,
            enable_submodules=enable_submodules,
            enable_lfs=enable_lfs,
        )
        if clone_result.resolved_ref:
            update_case(case_id, {"resolved_ref": clone_result.resolved_ref})
        if clone_result.used_fallback:
            publish_log(
                case_id,
                "system",
                f"[clone] Requested ref '{clone_result.requested_ref}' not found; fallback to '{clone_result.resolved_ref}'.",
            )
        elif clone_result.requested_ref in {None, "auto"} and clone_result.default_branch:
            publish_log(case_id, "system", f"[clone] Auto detected default branch '{clone_result.resolved_ref}'.")
        publish_log(case_id, "system", "[clone] Clone completed")
        has_submodules = (target_dir / ".gitmodules").exists()
        has_lfs_files = has_lfs(target_dir)
        clone_warnings: List[str] = []
        if has_submodules and enable_submodules:
            publish_log(case_id, "system", "[clone-post] Submodule update completed")
        elif has_submodules and not enable_submodules:
            publish_log(
                case_id,
                "system",
                "[clone-post] Submodules detected. Enable git.enable_submodule to fetch content.",
            )
            clone_warnings.append("SUBMODULES_DETECTED")
        elif enable_submodules:
            publish_log(case_id, "system", "[clone-post] Submodule support enabled (none detected)")
        if has_lfs_files and enable_lfs:
            publish_log(case_id, "system", "[clone-post] LFS pull completed")
        elif has_lfs_files and not enable_lfs:
            publish_log(
                case_id,
                "system",
                "[clone-post] LFS pointers detected. Enable git.enable_lfs to fetch content.",
            )
            clone_warnings.append("LFS_POINTERS_DETECTED")
        elif enable_lfs:
            publish_log(case_id, "system", "[clone-post] LFS support enabled (none detected)")
        try:
            commit_sha = (
                subprocess.check_output(
                    ["git", "-C", str(target_dir), "rev-parse", "HEAD"], text=True
                )
                .strip()
            )
        except Exception:
            commit_sha = None
        if commit_sha:
            update_case(case_id, {"commit_sha": commit_sha})
            publish_log(case_id, "system", f"Checked out commit {commit_sha}")
        _queue_manual_if_needed(case_id, auto_manual)

        inspection = inspect_repo(target_dir, REPO_SCAN_DEPTH)
        if compose_file:
            inspection.has_compose = True
        inspection_meta = {
            "repo_type": inspection.repo_type,
            "evidence": _truncate_list(list(inspection.evidence)),
            "has_dockerfile": inspection.has_dockerfile,
            "has_compose": inspection.has_compose,
        }
        publish_log(case_id, "system", f"[preflight] repo_inspector={json.dumps(inspection_meta)}")

        decision = select_strategy(run_mode, inspection)
        strategy_meta = {
            "strategy_selected": decision.strategy,
            "selection_reason": decision.selection_reason,
            "fallback_reason": decision.fallback_reason,
        }
        publish_log(case_id, "system", f"[preflight] strategy_selected={json.dumps(strategy_meta)}")

        update_case(
            case_id,
            {
                "repo_type": inspection.repo_type,
                "repo_evidence": inspection.evidence,
                "strategy_selected": decision.strategy,
                "strategy_reason": decision.selection_reason,
                "fallback_reason": decision.fallback_reason,
            },
        )

        if decision.strategy == "showcase":
            update_case(
                case_id,
                {
                    "status": "SHOWCASE_READY",
                    "stage": "showcase",
                    "mode": "showcase",
                    "error_code": None,
                    "error_message": None,
                    "preflight_meta": {
                        "stages": [],
                        "external_images_to_pull": [],
                        "warnings": clone_warnings,
                        "dockerfile_path": None,
                        "context_path": None,
                    },
                },
            )
            publish_log(case_id, "system", "[showcase] Showcase ready (manual generation queued)")
            return {"status": "SHOWCASE_READY"}

        if decision.strategy == "none":
            raise BuildError(
                "Dockerfile not found and run_mode=container requires Dockerfile/compose.",
                "DOCKERFILE_NOT_FOUND",
            )

        if decision.strategy == "compose":
            publish_log(case_id, "system", "[preflight] compose selected")
            compose_result = _run_compose_case(case_id, target_dir, compose_file, repo_url)
            cleanup_build_dir_on_exit = False
            update_case(
                case_id,
                {
                    "status": "RUNNING",
                    "stage": "run",
                    "mode": "compose",
                    "error_code": None,
                    "error_message": None,
                    "compose_file": compose_result.get("compose_file"),
                    "compose_project_name": compose_result.get("compose_project_name"),
                    "host_port": compose_result.get("host_port"),
                    "access_url": compose_result.get("access_url"),
                    "runtime": compose_result.get("runtime"),
                    "default_account": "Administrator / admin",
                    "repo_dir": str(target_dir),
                },
            )
            publish_log(
                case_id,
                "system",
                f"[compose] access_url={compose_result.get('access_url')} default_account=Administrator/admin",
            )
            return {"status": "RUNNING"}

        if decision.strategy == "generated":
            generated_path, generated_files, summary = generate_dockerfile(target_dir, inspection)
            update_case(
                case_id,
                {
                    "generated_files": generated_files,
                    "generated_dockerfile_path": str(generated_path.relative_to(target_dir)),
                },
            )
            publish_log(
                case_id,
                "system",
                f"[preflight] generated_dockerfile={json.dumps({'path': str(generated_path.relative_to(target_dir)), 'summary': summary})}",
            )
            publish_log(
                case_id,
                "system",
                f"[preflight] generated_files={json.dumps(generated_files)}",
            )
            dockerfile_path = str(generated_path.relative_to(target_dir))
            context_path = "."

        publish_log(case_id, "system", "[preflight] Resolving Dockerfile")
        try:
            (
                dockerfile_path_resolved,
                context_dir,
                candidates,
                used_auto,
                discovery_meta,
            ) = resolve_dockerfile(
                target_dir,
                dockerfile_path.strip() if dockerfile_path else None,
                context_path.strip() if context_path else None,
                DOCKERFILE_SEARCH_DEPTH,
            )
        except DockerfileAmbiguousError as exc:
            _log_dockerfile_discovery(case_id, exc.meta, None, target_dir)
            update_case(
                case_id,
                {
                    "preflight_meta": {
                        "stages": [],
                        "external_images_to_pull": [],
                        "warnings": clone_warnings,
                        "dockerfile_path": None,
                        "context_path": None,
                        "candidates": exc.candidates,
                    }
                },
            )
            raise BuildError(str(exc), "DOCKERFILE_AMBIGUOUS") from exc
        except FileNotFoundError as exc:
            meta = getattr(exc, "meta", None)
            if meta:
                _log_dockerfile_discovery(case_id, meta, None, target_dir)
            if run_mode == "auto":
                publish_log(
                    case_id,
                    "system",
                    "[preflight] Dockerfile missing; auto switching to showcase mode",
                )
                _queue_manual_if_needed(case_id, auto_manual)
                update_case(
                    case_id,
                    {
                        "status": "SHOWCASE_READY",
                        "stage": "showcase",
                        "mode": "showcase",
                        "fallback_reason": "DOCKERFILE_NOT_FOUND",
                        "error_code": None,
                        "error_message": None,
                        "preflight_meta": {
                            "stages": [],
                            "external_images_to_pull": [],
                            "warnings": clone_warnings,
                            "dockerfile_path": None,
                            "context_path": None,
                        },
                    },
                )
                return {"status": "SHOWCASE_READY"}
            update_case(
                case_id,
                {
                    "preflight_meta": {
                        "stages": [],
                        "external_images_to_pull": [],
                        "warnings": clone_warnings,
                        "dockerfile_path": None,
                        "context_path": None,
                    }
                },
            )
            raise BuildError(str(exc), "DOCKERFILE_NOT_FOUND") from exc

        if not context_dir.exists():
            raise BuildError(f"context_path not found: {context_dir}", "DOCKERFILE_NOT_FOUND")

        _log_dockerfile_discovery(case_id, discovery_meta, context_dir, target_dir)
        if used_auto and discovery_meta.get("selected_dockerfile"):
            publish_log(
                case_id,
                "system",
                f"[preflight] Dockerfile selected: {discovery_meta['selected_dockerfile']}",
            )
        elif dockerfile_path:
            publish_log(case_id, "system", f"[preflight] Dockerfile path: {dockerfile_path}")
        update_case(case_id, {"mode": "container"})

        try:
            dockerfile_rel = dockerfile_path_resolved.relative_to(context_dir)
        except ValueError as exc:
            raise BuildError("dockerfile_path must be under context_path", "DOCKERFILE_NOT_FOUND") from exc

        dockerfile_rel_str = str(dockerfile_rel)
        dockerfile_store = str(dockerfile_path_resolved.relative_to(target_dir))
        context_store = str(context_dir.relative_to(target_dir))
        context_store = context_store or "."
        update_case(
            case_id,
            {
                "resolved_dockerfile_path": dockerfile_store,
                "resolved_context_path": context_store,
                "docker_build_network": effective_network,
                "docker_no_cache": docker_no_cache,
                "docker_buildkit": buildkit_enabled,
                "build_arg_keys": sorted(docker_build_args.keys()),
                "git_submodules": enable_submodules,
                "git_lfs": enable_lfs,
            },
        )

        resolved_port = container_port or detect_exposed_port(dockerfile_path_resolved)

        existing = get_case(case_id) or {}
        env_keys = sorted(env.keys()) if env else sorted(existing.get("env_keys") or [])
        update_case(
            case_id,
            {
                "status": "BUILDING",
                "stage": "build",
                "container_port": resolved_port,
                "env_keys": env_keys,
            },
        )

        client = docker.from_env()
        proxy_host = _sanitize_proxy_host(
            docker_build_args.get("HTTP_PROXY")
            or docker_build_args.get("http_proxy")
            or docker_build_args.get("HTTPS_PROXY")
            or docker_build_args.get("https_proxy")
            or env.get("HTTP_PROXY")
            or env.get("http_proxy")
            or env.get("HTTPS_PROXY")
            or env.get("https_proxy")
            or ""
        )
        proxy_keys = {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        }
        build_proxy_injected = any(
            key in docker_build_args and docker_build_args[key] for key in proxy_keys
        )
        publish_log(
            case_id,
            "system",
            f"[preflight] proxy_injection={json.dumps({'build_proxy_injected': build_proxy_injected, 'runtime_proxy_injected': runtime_proxy_injected, 'proxy_host': proxy_host})}",
        )
        _LOGGER.info(
            "[preflight] proxy_injection=%s",
            json.dumps(
                {
                    "build_proxy_injected": build_proxy_injected,
                    "runtime_proxy_injected": runtime_proxy_injected,
                    "proxy_host": proxy_host,
                }
            ),
        )
        daemon_proxy_warning = None
        if network_settings.get("check_docker_proxy", True):
            daemon_proxy = _detect_docker_daemon_proxy(client)
            daemon_proxy_host = _sanitize_proxy_host(
                daemon_proxy.get("http_proxy") or daemon_proxy.get("https_proxy") or ""
            )
            daemon_proxy_configured = bool(daemon_proxy_host)
            publish_log(
                case_id,
                "system",
                f"[preflight] docker_daemon_proxy={json.dumps({'configured': daemon_proxy_configured, 'proxy_host': daemon_proxy_host})}",
            )
            _LOGGER.info(
                "[preflight] docker_daemon_proxy=%s",
                json.dumps(
                    {"configured": daemon_proxy_configured, "proxy_host": daemon_proxy_host}
                ),
            )
            if proxy_host and not daemon_proxy_configured:
                daemon_proxy_warning = "DOCKER_DAEMON_PROXY_UNSET"
        from_info = parse_dockerfile_from(dockerfile_path_resolved)
        images_to_pull, preflight_warnings = _plan_base_images(
            from_info.external_images,
            docker_build_args,
            from_info.arg_defaults,
        )
        if proxy_warning:
            preflight_warnings.append(proxy_warning)
        if discovery_meta.get("selected_backup"):
            preflight_warnings.append("DOCKERFILE_BACKUP_SELECTED")
        if clone_warnings:
            preflight_warnings.extend(clone_warnings)
        if network_warning:
            preflight_warnings.append(network_warning)
        if daemon_proxy_warning:
            preflight_warnings.append(daemon_proxy_warning)
        preflight_meta = {
            "stages": from_info.stages,
            "external_images_to_pull": images_to_pull,
            "warnings": preflight_warnings,
            "dockerfile_path": dockerfile_store,
            "context_path": context_store,
        }
        update_case(case_id, {"preflight_meta": preflight_meta})
        publish_log(case_id, "system", f"[preflight] stages={from_info.stages}")
        publish_log(
            case_id,
            "system",
            f"[preflight] external_images_to_pull={images_to_pull}",
        )
        if preflight_warnings:
            publish_log(case_id, "system", f"[preflight] warnings={preflight_warnings}")
        if from_info.requires_buildkit and not buildkit_enabled:
            raise BuildError(
                "BuildKit is required for this Dockerfile. Enable DOCKER_BUILDKIT=1 or daemon features.buildkit=true.",
                "BUILDKIT_REQUIRED",
            )
        _pull_base_images(
            client,
            images_to_pull,
            case_id=case_id,
        )
        tag = f"case-{case_id}:latest"
        update_case(case_id, {"image_tag": tag})

        publish_log(
            case_id,
            "system",
            f"[build] docker build (context={context_store}, dockerfile={dockerfile_rel_str}, network={effective_network}, no_cache={docker_no_cache}, buildkit={buildkit_enabled})",
        )
        for event in build_image(
            client,
            context_dir,
            tag,
            dockerfile=dockerfile_path_resolved,
            buildargs=docker_build_args,
            network_mode=effective_network,
            nocache=docker_no_cache,
            use_buildkit=buildkit_enabled,
        ):
            log_event(case_id, event)

        update_case(case_id, {"status": "STARTING", "stage": "run"})

        labels = {CASE_LABEL_KEY: case_id, CASE_LABEL_MANAGED: "true"}
        if PORT_MODE == "dynamic":
            container_id, host_port = run_container_dynamic(
                client,
                tag,
                resolved_port,
                env,
                labels=labels,
            )
        else:
            container_id = None
            last_error: Optional[Exception] = None
            tried_ports: set[int] = set()
            for attempt in range(1, 4):
                host_port = reserve_port(case_id, exclude=tried_ports)
                port_reserved = True
                tried_ports.add(host_port)
                try:
                    container_id = run_container_fixed(
                        client,
                        tag,
                        resolved_port,
                        host_port,
                        env,
                        labels=labels,
                    )
                    last_error = None
                    break
                except docker.errors.APIError as exc:
                    last_error = exc
                    release_port(host_port)
                    port_reserved = False
                    if _is_port_in_use_error(str(exc)):
                        publish_log(
                            case_id,
                            "system",
                            f"[run] Port {host_port} already allocated, retrying ({attempt}/3)",
                            level="ERROR",
                        )
                        continue
                    raise
            if container_id is None:
                raise BuildError("Port already allocated or reserved", "PORT_IN_USE") from last_error

        wait_for_container_running(client, container_id, STARTUP_TIMEOUT_SECONDS)
        access_url = f"http://{PUBLIC_HOST}:{host_port}"
        container_started = True
        update_case(
            case_id,
            {
                "status": "RUNNING",
                "stage": "run",
                "container_id": container_id,
                "host_port": host_port,
                "access_url": access_url,
                "runtime": {
                    "container_id": container_id,
                    "host_port": host_port,
                    "access_url": access_url,
                    "started_at": _now_ts(),
                    "exited_at": None,
                },
            },
        )
        publish_log(case_id, "system", f"[run] Container started: {access_url}")
        launch_log_stream(case_id, container_id)
        if auto_analyze:
            analyze_case.delay(case_id)
        return {"container_id": container_id, "host_port": host_port}
    except Exception as exc:  # noqa: BLE001
        error_code, detail = classify_error(exc)
        error_message = _format_error_message(error_code, detail)
        existing = get_case(case_id) or {}
        stage = existing.get("stage") or "system"
        update_case(
            case_id,
            {
                "status": "FAILED",
                "stage": stage,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        publish_log(case_id, "system", f"ERROR [{error_code}]: {error_message}", level="ERROR")
        raise
    finally:
        if cleanup_build_dir_on_exit:
            cleanup_build_dir(target_dir)
        if PORT_MODE != "dynamic" and port_reserved and not container_started:
            release_port(host_port)


@celery_app.task(name="follow_container_logs")
def follow_container_logs(case_id: str, container_id: str) -> None:
    stream_container_logs(case_id, container_id)


@celery_app.task(
    name="analyze_case",
    time_limit=ANALYZE_TIMEOUT_SECONDS,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": WORKER_TASK_MAX_RETRIES},
)
def analyze_case(case_id: str, force: bool = False, mode: str = "light") -> None:
    cache_key = None
    try:
        existing = get_case(case_id) or {}
        if not existing:
            return
        repo_url = existing.get("repo_url")
        commit_sha = existing.get("commit_sha") or "unknown"
        if repo_url:
            cache_key = repo_cache_key(repo_url, commit_sha)
        update_case(
            case_id,
            {
                "analyze_status": "RUNNING",
                "report_ready": False,
                "analyze_error_code": None,
                "analyze_error_message": None,
            },
        )
        publish_log(case_id, "analyze", f"ANALYZE_START force={force} mode={mode}")

        if not repo_url:
            raise AnalysisFailure("ANALYZE_REPO_NOT_FOUND", "Missing repo_url")
        ref = existing.get("resolved_ref") or existing.get("ref") or existing.get("branch")
        env_keys = existing.get("env_keys") or []
        enable_submodules = existing.get("git_submodules")
        enable_lfs = existing.get("git_lfs")
        if enable_submodules is None:
            enable_submodules = GIT_ENABLE_SUBMODULES
        if enable_lfs is None:
            enable_lfs = GIT_ENABLE_LFS

        repo_dir = existing.get("repo_dir")
        preferred_path = Path(repo_dir) if repo_dir else None

        outcome = run_analysis(
            case_id=case_id,
            repo_url=repo_url,
            ref=ref,
            env_keys=env_keys,
            commit_sha=commit_sha,
            preferred_repo_path=preferred_path,
            enable_submodules=bool(enable_submodules),
            enable_lfs=bool(enable_lfs),
            force=force,
            mode=mode,
            log=lambda line: publish_log(case_id, "analyze", line),
        )

        update_case(
            case_id,
            {
                "analyze_status": "FINISHED",
                "report_ready": True,
                "report_cached": outcome.cache_hit,
                "commit_sha": outcome.commit_sha,
                "analyze_error_code": None,
                "analyze_error_message": None,
            },
        )
        publish_log(case_id, "analyze", "ANALYZE_DONE")
    except AnalysisFailure as exc:
        update_case(
            case_id,
            {
                "analyze_status": "FAILED",
                "report_ready": False,
                "analyze_error_code": exc.code,
                "analyze_error_message": exc.message,
            },
        )
        publish_log(case_id, "analyze", f"ERROR [{exc.code}]: {exc.message}", level="ERROR")
        raise
    except LLMError as exc:
        error_message = str(exc)
        update_case(
            case_id,
            {
                "analyze_status": "FAILED",
                "report_ready": False,
                "analyze_error_code": "ANALYZE_LLM_FAILED",
                "analyze_error_message": error_message,
            },
        )
        publish_log(case_id, "analyze", f"ERROR [ANALYZE_LLM_FAILED]: {error_message}", level="ERROR")
        raise
    except SoftTimeLimitExceeded as exc:
        update_case(
            case_id,
            {
                "analyze_status": "FAILED",
                "analyze_error_code": "TIMEOUT_ANALYZE",
                "analyze_error_message": str(exc),
            },
        )
        publish_log(case_id, "system", "ERROR [TIMEOUT_ANALYZE]: analyze timed out", level="ERROR")
        raise
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        update_case(
            case_id,
            {
                "analyze_status": "FAILED",
                "report_ready": False,
                "analyze_error_code": "UNEXPECTED_ERROR",
                "analyze_error_message": error_message,
            },
        )
        publish_log(case_id, "analyze", f"ERROR [UNEXPECTED_ERROR]: {error_message}", level="ERROR")
        raise
    finally:
        if cache_key:
            release_analyze_lock(cache_key)


@celery_app.task(
    name="visualize_case",
    time_limit=VISUALIZE_TIMEOUT_SECONDS,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": WORKER_TASK_MAX_RETRIES},
)
def visualize_case(case_id: str, force: bool = False, kinds: Optional[List[str]] = None) -> None:
    cache_key = None
    try:
        existing = get_case(case_id) or {}
        if not existing:
            return
        repo_url = existing.get("repo_url")
        commit_sha = existing.get("commit_sha")
        if repo_url:
            cache_key = visual_cache_key(repo_url, commit_sha or "unknown")
        update_case(
            case_id,
            {
                "visual_status": "RUNNING",
                "visual_ready": False,
                "visual_error_code": None,
                "visual_error_message": None,
            },
        )
        publish_log(case_id, "visualize", f"VISUALIZE_START force={force} kinds={kinds}")

        if not repo_url:
            raise VisualFailure("VISUALIZE_REPORT_NOT_READY", "Missing repo_url")

        ref = existing.get("resolved_ref") or existing.get("ref") or existing.get("branch")
        env_keys = existing.get("env_keys") or []
        enable_submodules = existing.get("git_submodules")
        enable_lfs = existing.get("git_lfs")
        if enable_submodules is None:
            enable_submodules = GIT_ENABLE_SUBMODULES
        if enable_lfs is None:
            enable_lfs = GIT_ENABLE_LFS
        repo_dir = existing.get("repo_dir")
        preferred_path = Path(repo_dir) if repo_dir else None

        outcome = run_visualize(
            case_id=case_id,
            repo_url=repo_url,
            commit_sha=commit_sha,
            force=force,
            kinds=kinds,
            log=lambda line: publish_log(case_id, "visualize", line),
            repo_dir=preferred_path,
            ref=ref,
            env_keys=env_keys,
            enable_submodules=bool(enable_submodules),
            enable_lfs=bool(enable_lfs),
        )

        visual_status = str(outcome.payload.get("status") or "SUCCESS").upper()
        update_case(
            case_id,
            {
                "visual_status": visual_status,
                "visual_ready": True,
                "visual_cached": outcome.cache_hit,
                "commit_sha": outcome.commit_sha,
                "visual_error_code": None,
                "visual_error_message": None,
            },
        )
        publish_log(case_id, "visualize", f"VISUALIZE_DONE status={visual_status}")
    except VisualFailure as exc:
        update_case(
            case_id,
            {
                "visual_status": "FAILED",
                "visual_ready": False,
                "visual_error_code": exc.code,
                "visual_error_message": exc.message,
            },
        )
        publish_log(case_id, "visualize", f"ERROR [{exc.code}]: {exc.message}", level="ERROR")
        raise
    except SoftTimeLimitExceeded as exc:
        update_case(
            case_id,
            {
                "visual_status": "FAILED",
                "visual_error_code": "TIMEOUT_VISUALIZE",
                "visual_error_message": str(exc),
            },
        )
        publish_log(case_id, "system", "ERROR [TIMEOUT_VISUALIZE]: visualize timed out", level="ERROR")
        raise
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        update_case(
            case_id,
            {
                "visual_status": "FAILED",
                "visual_ready": False,
                "visual_error_code": "UNEXPECTED_ERROR",
                "visual_error_message": error_message,
            },
        )
        publish_log(case_id, "visualize", f"ERROR [UNEXPECTED_ERROR]: {error_message}", level="ERROR")
        raise
    finally:
        if cache_key:
            release_visualize_lock(cache_key)


@celery_app.task(
    name="generate_manual",
    time_limit=TIMEOUT_MANUAL_SECONDS,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": WORKER_TASK_MAX_RETRIES},
)
def generate_manual_task(case_id: str) -> dict:
    start = time.time()
    data = get_case(case_id) or {}
    repo_url = data.get("repo_url")
    ref = normalize_ref(data.get("resolved_ref") or data.get("ref") or data.get("branch"))
    env_keys = data.get("env_keys") or []
    enable_submodules = data.get("git_submodules")
    enable_lfs = data.get("git_lfs")
    if enable_submodules is None:
        enable_submodules = GIT_ENABLE_SUBMODULES
    if enable_lfs is None:
        enable_lfs = GIT_ENABLE_LFS
    if not repo_url:
        error_code = "MANUAL_GENERATION_FAILED"
        error_message = _format_error_message(error_code, "Missing repo_url")
        set_manual_status(case_id, "FAILED", error_code, error_message)
        update_case(
            case_id,
            {
                "manual_status": "FAILED",
                "manual_error_code": error_code,
                "manual_error_message": error_message,
            },
        )
        return {"case_id": case_id, "status": "FAILED"}

    previous_manual, _ = get_manual(case_id)
    set_manual_status(case_id, "RUNNING")
    update_case(case_id, {"manual_status": "RUNNING"})
    target_dir = Path(MANUAL_ROOT) / case_id
    try:
        publish_log(case_id, "system", "Manual generation started")
        clone_result = clone_repo(
            repo_url,
            ref,
            target_dir,
            enable_submodules=bool(enable_submodules),
            enable_lfs=bool(enable_lfs),
        )
        if clone_result.used_fallback:
            publish_log(
                case_id,
                "system",
                f"Manual clone fallback to '{clone_result.resolved_ref}' after missing ref '{clone_result.requested_ref}'.",
            )
        elif clone_result.requested_ref in {None, "auto"} and clone_result.default_branch:
            publish_log(case_id, "system", f"Manual clone auto detected '{clone_result.resolved_ref}'.")
        manual_md, meta = generate_manual(
            target_dir,
            env_keys=env_keys,
            repo_name=Path(repo_url).name,
            tree_depth=MANUAL_TREE_DEPTH,
            readme_max_chars=MANUAL_MAX_README_CHARS,
            previous_manual=previous_manual,
        )
        set_manual(case_id, manual_md, meta)
        set_manual_status(case_id, "SUCCESS", generated_at=meta.get("generated_at"))
        update_case(
            case_id,
            {
                "manual_status": "SUCCESS",
                "manual_generated_at": meta.get("generated_at"),
                "manual_error_code": None,
                "manual_error_message": None,
                "manual_meta": meta,
            },
        )
        if (data.get("mode") or "").lower() == "showcase":
            update_case(case_id, {"status": "SHOWCASE_READY", "stage": "showcase"})
        warnings = meta.get("warnings") or []
        publish_log(
            case_id,
            "system",
            f"Manual generation finished time_cost_ms={meta.get('time_cost_ms')} warnings={','.join(warnings) or 'none'}",
        )
        record_manual_stats(int((time.time() - start) * 1000), True)
        return {"case_id": case_id, "status": "SUCCESS"}
    except Exception as exc:  # noqa: BLE001
        error_code, detail = classify_manual_error(exc)
        error_message = _format_error_message(error_code, detail)
        set_manual_status(case_id, "FAILED", error_code, error_message)
        update_case(
            case_id,
            {
                "manual_status": "FAILED",
                "manual_error_code": error_code,
                "manual_error_message": error_message,
            },
        )
        if (data.get("mode") or "").lower() == "showcase":
            update_case(
                case_id,
                {
                    "status": "SHOWCASE_FAILED",
                    "stage": "showcase",
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )
        publish_log(case_id, "system", f"ERROR [{error_code}]: {error_message}", level="ERROR")
        record_manual_stats(int((time.time() - start) * 1000), False)
        raise
    finally:
        cleanup_build_dir(target_dir)
