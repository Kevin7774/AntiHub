import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib import request

API = os.getenv("API_BASE", "http://127.0.0.1:8010")
LOG_DIR = Path(os.getenv("LOG_DIR", ".devlogs"))
LOG_DIR.mkdir(exist_ok=True)

REPO_REMOTE = "https://github.com/Kevin7774/wechat-miniprogram-card"
REPO_MULTI_STAGE = str(Path("test-repos/multi-stage-stage-ref").resolve())
REPO_BUILDKIT = str(Path("test-repos/buildkit-required").resolve())
REPO_SUBDIR = str(Path("test-repos/docker-subdir").resolve())
REPO_AMBIGUOUS = str(Path("test-repos/docker-ambiguous").resolve())
REPO_NO_DOCKER = str(Path("test-repos/no-dockerfile").resolve())


def post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_empty(url):
    req = request.Request(url, method="POST")
    with request.urlopen(req, timeout=30) as resp:
        return resp.status


def get_json(url):
    with request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_text(url):
    with request.urlopen(url, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", errors="ignore")


def is_api_ready():
    try:
        with request.urlopen(f"{API}/healthz", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_backend():
    if is_api_ready():
        cleanup_managed_containers()
        cleanup_cases()
        return
    root = Path(__file__).resolve().parents[1]
    dev_script = root / "dev.sh"
    if not dev_script.exists():
        raise RuntimeError("dev.sh not found; cannot auto-start backend")
    subprocess.run([str(dev_script), "up"], check=True)
    for _ in range(20):
        if is_api_ready():
            cleanup_managed_containers()
            cleanup_cases()
            return
        time.sleep(1)
    raise RuntimeError("Backend did not become ready after dev.sh up")


def cleanup_managed_containers():
    if not shutil.which("docker"):
        return
    try:
        output = subprocess.check_output(
            ["docker", "ps", "--filter", "label=antihub.managed=true", "-q"],
            text=True,
        ).strip()
    except Exception:
        return
    if not output:
        return
    ids = [item for item in output.splitlines() if item.strip()]
    if not ids:
        return
    subprocess.run(
        ["docker", "rm", "-f", *ids],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cleanup_cases():
    try:
        data = get_json(f"{API}/cases?size=200&include_archived=true")
    except Exception:
        return
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return
    stop_statuses = {"CLONING", "BUILDING", "STARTING", "RUNNING", "ANALYZING"}
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").upper()
        case_id = item.get("case_id")
        if status in stop_statuses and case_id:
            try:
                post_empty(f"{API}/cases/{case_id}/stop")
            except Exception:
                pass


def get_logs(case_id, limit=200):
    return get_json(f"{API}/cases/{case_id}/logs?limit={limit}")


def wait_case(case_id, timeout=240):
    end = time.time() + timeout
    last = None
    terminal = {"RUNNING", "FAILED", "FINISHED", "STOPPED", "SHOWCASE_READY", "SHOWCASE_FAILED"}
    while time.time() < end:
        last = get_json(f"{API}/cases/{case_id}")
        status = (last.get("status") or "").upper()
        if status in terminal:
            return last
        time.sleep(2)
    return last


def wait_manual(case_id, timeout=240):
    end = time.time() + timeout
    last = None
    terminal = {"SUCCESS", "FAILED"}
    while time.time() < end:
        last = get_json(f"{API}/cases/{case_id}/manual/status")
        status = (last.get("status") or "").upper()
        if status in terminal:
            return last
        time.sleep(2)
    return last


def contains_log(logs, keyword):
    for entry in logs:
        if isinstance(entry, dict) and keyword in (entry.get("line") or ""):
            return True
    return False


def run_case(
    name,
    payload,
    expect_status=None,
    expect_error=None,
    expect_manual=None,
    expect_resolved_ref=None,
    expect_preflight_excludes=None,
    expect_preflight_includes=None,
    expect_log=None,
    expect_log_absent=None,
    skip=False,
    skip_reason=None,
):
    start = time.time()
    result = {"name": name, "payload": payload}
    if skip:
        duration_ms = int((time.time() - start) * 1000)
        result.update(
            {
                "case_id": None,
                "status": None,
                "manual_status": None,
                "logs": None,
                "duration_ms": duration_ms,
                "error_code": None,
                "pass": True,
                "skipped": True,
                "skip_reason": skip_reason or "skipped",
            }
        )
        return result
    case = post_json(f"{API}/cases", payload)
    case_id = case.get("case_id")
    result["case_id"] = case_id
    status = wait_case(case_id)
    logs = get_logs(case_id, limit=200)
    manual_status = None
    if expect_manual:
        manual_status = wait_manual(case_id)
    duration_ms = int((time.time() - start) * 1000)

    ok = True
    if expect_status:
        if isinstance(expect_status, (list, tuple, set)):
            ok = ok and status.get("status") in expect_status
        else:
            ok = ok and (status.get("status") == expect_status)
    if expect_error:
        ok = ok and (status.get("error_code") == expect_error)
    if expect_manual:
        ok = ok and (manual_status or {}).get("status") == expect_manual
    if expect_resolved_ref:
        ok = ok and status.get("resolved_ref") == expect_resolved_ref
    preflight = status.get("preflight_meta") or {}
    images = preflight.get("external_images_to_pull") or []
    for item in expect_preflight_excludes or []:
        ok = ok and item not in images
    for item in expect_preflight_includes or []:
        ok = ok and item in images
    if expect_log:
        ok = ok and contains_log(logs, expect_log)
    if expect_log_absent:
        ok = ok and not contains_log(logs, expect_log_absent)

    result.update(
        {
            "status": status,
            "manual_status": manual_status,
            "logs": logs,
            "duration_ms": duration_ms,
            "error_code": status.get("error_code"),
            "pass": ok,
        }
    )
    return result


def run_endpoint_test(name, func):
    start = time.time()
    result = {"name": name}
    try:
        endpoint_result = func()
        ok = True
        if isinstance(endpoint_result, dict):
            ok = bool(endpoint_result.get("pass", True))
        result.update(
            {
                "endpoint_results": endpoint_result,
                "pass": ok,
                "duration_ms": int((time.time() - start) * 1000),
            }
        )
        return result
    except Exception as exc:
        result.update(
            {
                "endpoint_results": {"error": str(exc)},
                "pass": False,
                "duration_ms": int((time.time() - start) * 1000),
            }
        )
        return result


def main():
    ensure_backend()
    results = []

    results.append(
        run_case(
            "multi_stage_stage_ref",
            {"repo_url": REPO_MULTI_STAGE},
            expect_status={"RUNNING", "FINISHED"},
            expect_preflight_excludes=["app-base"],
            expect_log_absent="Pulling base image app-base",
        )
    )

    results.append(
        run_case(
            "buildkit_required_pass",
            {"repo_url": REPO_BUILDKIT, "build": {"use_buildkit": True}},
            expect_status={"RUNNING", "FINISHED"},
        )
    )

    results.append(
        run_case(
            "buildkit_required_fail_fast",
            {"repo_url": REPO_BUILDKIT, "build": {"use_buildkit": False}},
            expect_status="FAILED",
            expect_error="BUILDKIT_REQUIRED",
        )
    )

    results.append(
        run_case(
            "dockerfile_subdir",
            {
                "repo_url": REPO_SUBDIR,
                "dockerfile_path": "app/Dockerfile",
                "context_path": "app",
            },
            expect_status="RUNNING",
        )
    )

    results.append(
        run_case(
            "dockerfile_ambiguous",
            {"repo_url": REPO_AMBIGUOUS},
            expect_status="FAILED",
            expect_error="DOCKERFILE_AMBIGUOUS",
        )
    )

    results.append(
        run_case(
            "no_dockerfile_deploy",
            {"repo_url": REPO_NO_DOCKER, "mode": "deploy"},
            expect_status="FAILED",
            expect_error="DOCKERFILE_NOT_FOUND",
        )
    )

    results.append(
        run_case(
            "no_dockerfile_showcase",
            {"repo_url": REPO_NO_DOCKER, "mode": "showcase"},
            expect_status="SHOWCASE_READY",
            expect_manual="SUCCESS",
        )
    )

    results.append(
        run_case(
            "wechat_miniprogram_showcase",
            {"repo_url": REPO_REMOTE, "ref": "auto", "mode": "showcase"},
            expect_status="SHOWCASE_READY",
            expect_manual="SUCCESS",
            expect_resolved_ref="main",
            skip=os.getenv("SKIP_NETWORK") in {"1", "true", "yes"},
            skip_reason="SKIP_NETWORK=1",
        )
    )

    results.append(
        run_endpoint_test(
            "docs_reachable",
            lambda: (lambda status: {"url": f"{API}/docs", "status": status, "pass": status == 200})(
                get_text(f"{API}/docs")[0]
            ),
        )
    )

    def _templates_check():
        data = get_json(f"{API}/templates")
        template_ids = [item.get("template_id") for item in data if isinstance(item, dict)]
        has_default = "hello-world" in template_ids
        return {
            "count": len(data),
            "template_ids": template_ids,
            "pass": len(data) >= 3 and has_default,
        }

    results.append(run_endpoint_test("templates_list", _templates_check))

    def _template_case():
        case = post_json(
            f"{API}/cases",
            {"template_id": "hello-world", "mode": "showcase"},
        )
        case_id = case.get("case_id")
        status = wait_case(case_id)
        return {
            "case_id": case_id,
            "status": status.get("status"),
            "repo_url": status.get("repo_url"),
            "pass": status.get("status") in {"SHOWCASE_READY", "RUNNING", "FINISHED"},
        }

    results.append(run_endpoint_test("template_case_create", _template_case))

    results.append(
        run_endpoint_test(
            "plans_usage",
            lambda: (lambda plans, usage: {"plans_count": len(plans), "usage": usage, "pass": isinstance(plans, list) and isinstance(usage, dict)})(
                get_json(f"{API}/plans"),
                get_json(f"{API}/usage"),
            ),
        )
    )

    summary = {
        "results": results,
        "passed": all(item.get("pass") for item in results),
    }
    output_root = Path("integration_test_results.json").resolve()
    output_root.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    output_log = LOG_DIR / "integration_test_results.json"
    output_log.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
