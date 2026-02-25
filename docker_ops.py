import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import docker


def build_proxy_args(proxy_config: Dict[str, str]) -> Dict[str, str]:
    args: Dict[str, str] = {}
    http_proxy = (proxy_config.get("http_proxy") or "").strip()
    https_proxy = (proxy_config.get("https_proxy") or "").strip()
    no_proxy = (proxy_config.get("no_proxy") or "").strip()
    if http_proxy:
        args["HTTP_PROXY"] = http_proxy
        args["http_proxy"] = http_proxy
    if https_proxy:
        args["HTTPS_PROXY"] = https_proxy
        args["https_proxy"] = https_proxy
    if no_proxy:
        args["NO_PROXY"] = no_proxy
        args["no_proxy"] = no_proxy
    return args


def build_proxy_env(proxy_config: Dict[str, str]) -> Dict[str, str]:
    return build_proxy_args(proxy_config)


def detect_exposed_port(dockerfile: Path, default_port: int = 80) -> int:
    contents = dockerfile.read_text(encoding="utf-8")
    match = re.search(r"^EXPOSE\s+(\d+)", contents, re.MULTILINE)
    if match:
        return int(match.group(1))
    return default_port


def build_image(
    client: docker.DockerClient,
    path: Path,
    tag: str,
    dockerfile: Optional[Path] = None,
    buildargs: Optional[Dict[str, str]] = None,
    network_mode: Optional[str] = None,
    nocache: bool = False,
    use_buildkit: bool = True,
) -> Iterable[dict]:
    del client
    cmd = ["docker", "build", "-t", tag]
    if use_buildkit:
        cmd.extend(["--progress=plain"])
    if dockerfile:
        cmd.extend(["-f", str(dockerfile)])
    if network_mode:
        cmd.extend(["--network", network_mode])
    if nocache:
        cmd.append("--no-cache")
    for key, value in (buildargs or {}).items():
        cmd.extend(["--build-arg", f"{key}={value}"])
    cmd.append(str(path))
    env = os.environ.copy()
    env["DOCKER_BUILDKIT"] = "1" if use_buildkit else "0"

    events: List[dict] = []
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    ) as proc:
        if proc.stdout:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                event = {"stream": line}
                events.append(event)
                if len(events) > 2000:
                    events.pop(0)
                yield event
        return_code = proc.wait()
    if return_code != 0:
        tail = events[-80:]
        message = "\n".join(item["stream"] for item in tail if item.get("stream")) or (
            f"docker build failed with exit code {return_code}"
        )
        raise docker.errors.BuildError(message, tail)
    return []


def run_container_dynamic(
    client: docker.DockerClient,
    image: str,
    container_port: int,
    environment: Optional[Dict[str, str]] = None,
    labels: Optional[Dict[str, str]] = None,
) -> Tuple[str, int]:
    container = client.containers.run(
        image,
        detach=True,
        ports={f"{container_port}/tcp": None},
        environment=environment or {},
        labels=labels or {},
    )
    container.reload()
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
    binding = ports.get(f"{container_port}/tcp")
    if not binding:
        raise RuntimeError("Failed to resolve mapped host port")
    host_port = int(binding[0]["HostPort"])
    return container.id, host_port


def run_container_fixed(
    client: docker.DockerClient,
    image: str,
    container_port: int,
    host_port: int,
    environment: Optional[Dict[str, str]] = None,
    labels: Optional[Dict[str, str]] = None,
) -> str:
    container = client.containers.run(
        image,
        detach=True,
        ports={f"{container_port}/tcp": host_port},
        environment=environment or {},
        labels=labels or {},
    )
    return container.id


def cleanup_build_dir(target_dir: Path) -> None:
    if target_dir.exists():
        import shutil
        shutil.rmtree(target_dir, ignore_errors=True)


def wait_for_container_running(
    client: docker.DockerClient,
    container_id: str,
    timeout_seconds: int,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        container = client.containers.get(container_id)
        container.reload()
        status = container.status
        if status == "running":
            return
        if status in {"exited", "dead"}:
            raise RuntimeError("Container exited during startup")
        time.sleep(1)
    raise TimeoutError("Container startup timed out")
