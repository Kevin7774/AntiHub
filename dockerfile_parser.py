import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

FROM_PATTERN = re.compile(r"^FROM\s+(.+)$", re.IGNORECASE)
ARG_PATTERN = re.compile(r"^ARG\s+(.+)$", re.IGNORECASE)


@dataclass
class DockerfileFromInfo:
    external_images: List[str]
    stages: List[str]
    requires_buildkit: bool
    arg_defaults: Dict[str, str]


def _tokenize_from(rest: str) -> List[str]:
    try:
        return shlex.split(rest, posix=True)
    except ValueError:
        return rest.split()


def parse_dockerfile_from(dockerfile_path: Path) -> DockerfileFromInfo:
    external_images: List[str] = []
    stages: List[str] = []
    arg_defaults: Dict[str, str] = {}
    known_stage_keys = set()
    seen_images = set()
    requires_buildkit = False
    try:
        contents = dockerfile_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return DockerfileFromInfo(external_images, stages, requires_buildkit, arg_defaults)

    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("# syntax=docker/dockerfile:"):
            requires_buildkit = True
        if lower.startswith("run ") and "--mount=" in lower:
            requires_buildkit = True
        if "--platform=" in lower or "$buildplatform" in lower or "$targetplatform" in lower:
            requires_buildkit = True

        if line.startswith("#"):
            continue

        arg_match = ARG_PATTERN.match(line)
        if arg_match:
            rest = arg_match.group(1).strip()
            if rest:
                token = rest.split()[0]
                name, _, value = token.partition("=")
                if name and value:
                    arg_defaults[name] = value
            continue

        match = FROM_PATTERN.match(line)
        if not match:
            continue

        rest = match.group(1).strip()
        tokens = _tokenize_from(rest)
        if not tokens:
            continue

        index = 0
        while index < len(tokens) and tokens[index].startswith("--"):
            token = tokens[index].lower()
            if token.startswith("--platform"):
                requires_buildkit = True
                if token == "--platform" and index + 1 < len(tokens):
                    index += 1
            index += 1
        if index >= len(tokens):
            continue
        image_or_stage = tokens[index]
        index += 1

        stage = None
        if index + 1 < len(tokens) and tokens[index].lower() == "as":
            stage = tokens[index + 1]

        image_key = image_or_stage.lower()
        is_stage_reference = image_key in known_stage_keys
        if stage:
            stage_name = stage.strip()
            stage_key = stage_name.lower()
            if stage_key not in known_stage_keys:
                stages.append(stage_name)
                known_stage_keys.add(stage_key)

        if image_or_stage.lower() == "scratch":
            continue
        if not is_stage_reference:
            if image_or_stage not in seen_images:
                external_images.append(image_or_stage)
                seen_images.add(image_or_stage)

    return DockerfileFromInfo(external_images, stages, requires_buildkit, arg_defaults)


def parse_base_images(dockerfile_path: Path) -> List[str]:
    return parse_dockerfile_from(dockerfile_path).external_images
