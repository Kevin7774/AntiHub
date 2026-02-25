import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict

from openclaw.skills.github_fetch import github_fetch


class SkillHandler(BaseHTTPRequestHandler):
    server_version = "OpenClawMock/0.1"

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/skills/run":
            self._send(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"ok": False, "error": "invalid_json"})
            return

        skill = data.get("skill")
        payload = data.get("input") or {}
        if skill != "github.fetch":
            self._send(400, {"ok": False, "error": "unknown_skill"})
            return

        result = github_fetch(payload)
        if not result.ok:
            self._send(
                200,
                {
                    "ok": False,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                },
            )
            return
        self._send(200, {"ok": True, "output": result.output})


def main() -> None:
    host = os.getenv("OPENCLAW_HOST", "0.0.0.0")
    port = int(os.getenv("OPENCLAW_PORT", "8787"))
    server = HTTPServer((host, port), SkillHandler)
    print(f"OpenClaw mock server listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
