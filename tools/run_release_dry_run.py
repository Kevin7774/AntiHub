#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "reports" / "release_dry_run_latest.md"


@dataclass
class CommandResult:
    command: str
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(command: list[str]) -> CommandResult:
    proc = subprocess.run(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return CommandResult(
        command=" ".join(command),
        returncode=int(proc.returncode),
        output=str(proc.stdout or "").strip(),
    )


def main() -> int:
    steps = [
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "pytest", "tests/test_auth_billing_api.py", "tests/test_billing_db_startup.py", "-q"],
        [sys.executable, "-m", "pytest", "tests/e2e/test_acceptance_flow.py", "-q"],
    ]
    results = [run_command(step) for step in steps]
    success = all(item.ok for item in results)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Release Dry Run",
        "",
        f"- Timestamp (UTC): `{datetime.now(timezone.utc).isoformat()}`",
        f"- Result: `{'PASS' if success else 'FAIL'}`",
        "",
        "## Commands",
    ]
    for index, item in enumerate(results, start=1):
        lines.extend(
            [
                f"{index}. `{item.command}`",
                f"   - status: `{item.returncode}`",
                "```text",
                item.output[-2000:] if item.output else "(no output)",
                "```",
            ]
        )
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[release-dry-run] report={REPORT_PATH}")
    print(f"[release-dry-run] result={'PASS' if success else 'FAIL'}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
