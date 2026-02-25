#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "__pycache__",
    ".venv",
    ".venv-local",
    ".pytest_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
}

EXCLUDED_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".mp4",
    ".webm",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pyc",
    ".so",
    ".bin",
}

PLACEHOLDER_MARKERS = (
    "your_",
    "example",
    "replace_me",
    "redacted",
    "masked",
    "dummy",
    "test",
    "sample",
    "placeholder",
    "changeme",
)

SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")
KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(openai[_-]?api[_-]?key|api[_-]?key|secret[_-]?key|access[_-]?key)\b\s*[:=]\s*[\"']([^\"']{16,})[\"']"
)


def iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        yield path


def is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    if lowered in {"null", "none", "true", "false"}:
        return True
    if lowered.startswith("${") and lowered.endswith("}"):
        return True
    if lowered.startswith("$"):
        return True
    if lowered.startswith("sk-****") or lowered.startswith("sk-xxx"):
        return True
    if "****" in lowered:
        return True
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    return False


def is_secret_like(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 20:
        return False
    lowered = stripped.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    has_alpha = any(char.isalpha() for char in stripped)
    has_digit = any(char.isdigit() for char in stripped)
    if not (has_alpha and has_digit):
        return False
    return True


def collect_issues(root: Path) -> list[tuple[Path, int, str]]:
    issues: list[tuple[Path, int, str]] = []
    for file_path in iter_files(root):
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            if "sk-" in line:
                for match in SK_PATTERN.findall(line):
                    if not is_placeholder(match):
                        issues.append((file_path, line_no, f"hardcoded key token: {match[:10]}..."))
            for _, value in KEY_VALUE_PATTERN.findall(line):
                cleaned = value.strip().strip("\"'")
                if is_placeholder(cleaned):
                    continue
                if not is_secret_like(cleaned):
                    continue
                issues.append((file_path, line_no, "suspicious credential assignment"))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan repository for hardcoded secrets.")
    parser.add_argument("--root", default=str(ROOT), help="Repository root path to scan.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    issues = collect_issues(root)
    if issues:
        print(f"[security-check] FAILED: found {len(issues)} potential secret(s)")
        for file_path, line_no, reason in issues:
            rel = file_path.relative_to(root)
            print(f"  - {rel}:{line_no}: {reason}")
        return 1

    print("[security-check] OK: no obvious hardcoded secrets found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
