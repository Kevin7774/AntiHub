import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from config import MERMAID_VALIDATE_TIMEOUT_SECONDS

ALLOWED_PREFIXES = (
    "graph",
    "flowchart",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
    "gantt",
    "journey",
    "pie",
    "mindmap",
    "timeline",
)


class MermaidValidationError(RuntimeError):
    pass


def _strip_fences(code: str) -> str:
    stripped = code.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```\w*", "", stripped)
        stripped = stripped.strip()
        if stripped.endswith("```"):
            stripped = stripped[: -3].strip()
    return stripped


def _syntax_check(code: str) -> Tuple[bool, str]:
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    if not lines:
        return False, "mermaid code is empty"
    first = lines[0]
    if not any(first.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        return False, f"unsupported mermaid diagram type: {first}"
    return True, "syntax check passed"


def validate_mermaid(code: str, output_dir: Path, index: int) -> Tuple[bool, str, Optional[str], str]:
    """
    Returns: ok, error_message, asset_path, method
    """
    cleaned = _strip_fences(code)
    if not cleaned:
        return False, "mermaid code is empty", None, "syntax"

    mmdc = shutil.which("mmdc")
    if not mmdc:
        ok, message = _syntax_check(cleaned)
        return ok, ("mmdc not available; " + message) if not ok else "syntax check passed", None, "syntax"

    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = output_dir / f"diagram_{index}.mmd"
    output_path = output_dir / f"diagram_{index}.svg"
    try:
        input_path.write_text(cleaned, encoding="utf-8")
        result = subprocess.run(
            [mmdc, "-i", str(input_path), "-o", str(output_path), "--quiet"],
            text=True,
            capture_output=True,
            timeout=MERMAID_VALIDATE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return False, f"mmdc failed: {exc}", None, "mmdc"

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "mmdc validation failed").strip()
        return False, error, None, "mmdc"
    if not output_path.exists():
        return False, "mmdc did not produce output", None, "mmdc"
    return True, "ok", str(output_path), "mmdc"
