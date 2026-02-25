import json
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    VISUAL_REMOTION_CODEC_MP4,
    VISUAL_REMOTION_CODEC_WEBM,
    VISUAL_REMOTION_FPS,
    VISUAL_REMOTION_HEIGHT,
    VISUAL_REMOTION_PROJECT,
    VISUAL_REMOTION_WIDTH,
    VISUAL_RENDER_WEBM,
)
from visualize.node_tools import find_node_bin


class RemotionRenderError(RuntimeError):
    pass


@dataclass
class RemotionRenderResult:
    files: List[str]
    duration_ms: int
    command: List[str]


def _ensure_remotion_project(project_dir: Path) -> None:
    if not project_dir.exists():
        raise RemotionRenderError(f"Remotion project missing: {project_dir}")
    if not (project_dir / "package.json").exists():
        raise RemotionRenderError(f"Remotion package.json missing: {project_dir}")


def _ensure_remotion_dependencies(project_dir: Path) -> Path:
    remotion_bin = project_dir / "node_modules" / ".bin" / "remotion"
    if remotion_bin.exists():
        return remotion_bin
    npm = find_node_bin("npm")
    if not npm:
        raise RemotionRenderError("npm not available; install Node.js to render video")
    result = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund"],
        cwd=str(project_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "npm install failed").strip()
        raise RemotionRenderError(f"npm install failed: {detail}")
    if not remotion_bin.exists():
        raise RemotionRenderError("remotion cli missing after npm install")
    return remotion_bin


def _copy_assets(public_root: Path, assets: Dict[str, Path]) -> Dict[str, str]:
    public_root.mkdir(parents=True, exist_ok=True)
    mapped: Dict[str, str] = {}
    for key, path in assets.items():
        if not path.exists():
            continue
        target = public_root / path.name
        shutil.copyfile(path, target)
        mapped[key] = str(target.relative_to(public_root.parent))
    return mapped


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def render_visual_video(
    visuals_dir: Path,
    props: Dict[str, Any],
    assets: Optional[Dict[str, Path]] = None,
    render_webm: Optional[bool] = None,
) -> RemotionRenderResult:
    project_dir = Path(VISUAL_REMOTION_PROJECT)
    _ensure_remotion_project(project_dir)
    remotion_bin = _ensure_remotion_dependencies(project_dir)

    render_webm = VISUAL_RENDER_WEBM if render_webm is None else render_webm
    public_root = project_dir / "public" / visuals_dir.name
    props = dict(props)
    if assets:
        props["assets"] = _copy_assets(public_root, assets)
    props["render"] = {
        "fps": VISUAL_REMOTION_FPS,
        "width": VISUAL_REMOTION_WIDTH,
        "height": VISUAL_REMOTION_HEIGHT,
    }
    props_path = visuals_dir / "remotion_props.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    base_command = [
        str(remotion_bin),
        "render",
        "src/index.tsx",
        "VisualPack",
    ]
    files: List[str] = []
    start = time.time()

    def run_render(output_name: str, codec: str) -> None:
        output_path = visuals_dir / output_name
        if output_path.exists():
            output_path.unlink()
        port = _pick_free_port()
        cmd = base_command + [
            str(output_path),
            "--props",
            json.dumps(props, ensure_ascii=False),
            "--codec",
            codec,
            "--port",
            str(port),
        ]
        subprocess.run(
            cmd,
            cwd=str(project_dir),
            check=True,
            capture_output=True,
            text=True,
        )
        files.append(output_path.name)

    try:
        run_render("visual_pack.mp4", VISUAL_REMOTION_CODEC_MP4)
        if render_webm:
            run_render("visual_pack.webm", VISUAL_REMOTION_CODEC_WEBM)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RemotionRenderError(detail) from exc

    duration_ms = int((time.time() - start) * 1000)
    return RemotionRenderResult(files=files, duration_ms=duration_ms, command=base_command)
