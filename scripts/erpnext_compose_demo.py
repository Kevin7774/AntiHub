import json
import socket
import subprocess
import time
import urllib.request

API_BASE = "http://127.0.0.1:8010"


def _request(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)


def _stream_logs(case_id: str, last_ts: float) -> float:
    try:
        logs = _request("GET", f"{API_BASE}/cases/{case_id}/logs?limit=200")
    except Exception:
        return last_ts
    for entry in logs:
        ts = float(entry.get("ts") or 0)
        if ts <= last_ts:
            continue
        stream = entry.get("stream") or "log"
        line = entry.get("line") or ""
        if line:
            print(f"[{stream}] {line}")
        if ts > last_ts:
            last_ts = ts
    return last_ts


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _show_port_owner(port: int) -> None:
    try:
        result = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}} {{.Ports}}"],
            text=True,
        )
        lines = [line for line in result.splitlines() if f":{port}->" in line]
        if lines:
            print(f"port {port} occupied by docker: {lines[0]}")
            return
    except Exception:
        pass
    print(f"port {port} appears occupied; check with: ss -ltnp | grep ':{port}'")


def main() -> None:
    if _port_open(8080):
        _show_port_owner(8080)
        raise SystemExit("port 8080 already in use")
    payload = {
        "repo_url": "https://github.com/frappe/frappe_docker",
        "run_mode": "compose",
        "compose_file": "pwd.yml",
    }
    case = _request("POST", f"{API_BASE}/cases", payload)
    case_id = case["case_id"]
    print(f"case_id={case_id}")

    deadline = time.time() + 3600
    status = None
    last_ts = 0.0
    while time.time() < deadline:
        case = _request("GET", f"{API_BASE}/cases/{case_id}")
        status = case.get("status")
        last_ts = _stream_logs(case_id, last_ts)
        if status in {"RUNNING", "FAILED"}:
            break
        time.sleep(5)
    print(f"status={status}")
    if status != "RUNNING":
        error_code = case.get("error_code")
        error_message = case.get("error_message")
        raise SystemExit(f"case not running: {error_code} {error_message}")

    access_url = case.get("access_url") or case.get("runtime", {}).get("access_url")
    print(f"access_url={access_url}")
    ping = _request("GET", f"{access_url}/api/method/ping")
    print(f"ping={ping}")

    _request("POST", f"{API_BASE}/cases/{case_id}/stop")
    print("cleanup=done")


if __name__ == "__main__":
    main()
