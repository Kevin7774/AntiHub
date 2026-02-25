#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "reports" / "failure_drill_latest.md"


@dataclass
class StepResult:
    name: str
    command: str
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run(command: list[str], *, env: dict[str, str] | None = None) -> StepResult:
    proc = subprocess.run(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return StepResult(
        name=" ".join(command[:3]),
        command=" ".join(command),
        returncode=int(proc.returncode),
        output=str(proc.stdout or "").strip(),
    )


def _wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main() -> int:
    env = dict(os.environ)
    env.setdefault("APP_ENV", "dev")
    env["PAYMENT_WEBHOOK_SECRET"] = env.get("PAYMENT_WEBHOOK_SECRET", "drill_webhook_secret")
    env.setdefault("AUTH_ENABLED", "true")
    env.setdefault("AUTH_TOKEN_SECRET", "drill_token_secret_please_change")
    env.setdefault("AUTH_USERS_JSON", '{"admin":{"password":"change_me","role":"admin"}}')

    results: list[StepResult] = []

    uvicorn_cmd = [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8010"]
    server = subprocess.Popen(
        uvicorn_cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if not _wait_port("127.0.0.1", 8010):
            raise RuntimeError("backend failed to start for failure drill")

        results.append(
            _run(
                [
                    sys.executable,
                    "tools/chaos_suite.py",
                    "--attack-count",
                    "20",
                    "--replay-concurrency",
                    "10",
                    "--webhook-url",
                    "http://127.0.0.1:8010/billing/webhooks/payment",
                    "--health-url",
                    "http://127.0.0.1:8010/health/billing",
                ],
                env=env,
            )
        )
        results.append(
            _run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_billing_rate_limit.py::test_rate_limiter_requires_redis_in_production",
                    "tests/test_global_exception_handler.py::test_unhandled_exceptions_are_normalized",
                    "-q",
                ],
                env=env,
            )
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5.0)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    ok = all(item.ok for item in results)
    lines = [
        "# Failure Drill Report",
        "",
        f"- Timestamp (UTC): `{timestamp}`",
        f"- Result: `{'PASS' if ok else 'FAIL'}`",
        "",
        "## Steps",
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
    print(f"[drill] report={REPORT_PATH}")
    print(f"[drill] result={'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
