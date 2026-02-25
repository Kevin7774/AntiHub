#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

EXCLUDE_DIR_NAMES = {".git", ".venv", "node_modules", "__pycache__"}
EXCLUDE_PREFIXES = (
    ".git/",
    ".venv/",
    "node_modules/",
    "frontend/node_modules/",
)
EXCLUDE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.development.local",
    ".env.test.local",
    ".env.production.local",
}


def _should_skip(rel: str, output_name: str) -> bool:
    normalized = rel.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.startswith("/"):
        normalized = normalized[1:]
    if not normalized:
        return False
    if normalized == output_name:
        return True
    if normalized in EXCLUDE_FILE_NAMES:
        return True
    if any(normalized == item.rstrip("/") or normalized.startswith(item) for item in EXCLUDE_PREFIXES):
        return True
    parts = normalized.split("/")
    return any(part in EXCLUDE_DIR_NAMES for part in parts)


def build_release_zip(root: Path, output_path: Path) -> tuple[int, int]:
    count = 0
    total_bytes = 0
    output_name = output_path.name
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(root).as_posix()
            if _should_skip(rel, output_name):
                continue
            archive.write(path, arcname=rel)
            count += 1
            total_bytes += int(path.stat().st_size)
    return count, total_bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build AntiHub release zip artifact")
    parser.add_argument(
        "--output",
        default="antihub-v2.0-mvp.zip",
        help="output zip filename (relative to repository root)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parent.parent
    output = (root / str(args.output)).resolve()
    if output.exists():
        output.unlink()
    count, total_bytes = build_release_zip(root, output)
    print(f"[release] output={output}")
    print(f"[release] files={count} bytes={total_bytes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
