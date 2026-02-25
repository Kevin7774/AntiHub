import os
import re
import shutil
from pathlib import Path
from typing import Optional


def _version_key(path: Path) -> tuple:
    name = path.parent.parent.name
    parts = [int(part) for part in re.findall(r"\d+", name)]
    return tuple(parts)


def find_node_bin(name: str) -> Optional[str]:
    direct = shutil.which(name)
    if direct:
        return direct

    candidates: list[Path] = []
    nvm_dir = os.environ.get("NVM_DIR") or os.path.expanduser("~/.nvm")
    versions_dir = Path(nvm_dir) / "versions" / "node"
    if versions_dir.exists():
        candidates.extend(versions_dir.glob(f"v*/bin/{name}"))

    volta_bin = Path(os.path.expanduser("~/.volta/bin")) / name
    if volta_bin.exists():
        candidates.append(volta_bin)

    if not candidates:
        return None

    best = max(candidates, key=_version_key)
    return str(best)
