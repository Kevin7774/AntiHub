import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import worker
from analyze.report_store import ReportStore
from analyze.service import AnalysisOutcome, run_analysis
from analyze.signals import extract_signals
from main import build_case_response
from storage import decode_log_entry


class DummyLLM:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.called = False
        self.business_called = False

    def generate(self, signals):
        self.called = True
        return self.payload

    def generate_business_summary(self, payload):
        self.business_called = True
        return "{\"slogan\":\"Demo\",\"business_values\":[\"v1\",\"v2\",\"v3\"],\"business_scenarios\":[\"s1\",\"s2\"],\"readme_marketing_cards\":[{\"title\":\"t\",\"description\":\"d\"}]}"

    def translate_markdown(self, markdown: str) -> str:
        return markdown

    def repair_mermaid(self, mermaid_code: str, error_message: str) -> str:
        return mermaid_code


def test_cache_hit_skips_llm(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Demo", encoding="utf-8")

    repo_url = "https://example.com/demo.git"
    commit_sha = "abc123"
    store = ReportStore(root=tmp_path / "reports")
    store.save_report(
        repo_url,
        commit_sha,
        {
            "case_id": "c_demo",
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "created_at": 1.0,
            "markdown": "cached",
            "mermaids": [],
            "assets": [],
            "validation": {"summary": {"ok": True}},
            "business_summary": {
                "slogan": "Cached",
                "business_values": ["a", "b", "c"],
                "business_scenarios": ["s1", "s2"],
                "readme_marketing_cards": [{"title": "t", "description": "d"}],
            },
        },
    )

    dummy = DummyLLM("{\"markdown\":\"# Hi\",\"mermaids\":[]}")
    outcome = run_analysis(
        case_id="c_demo",
        repo_url=repo_url,
        ref=None,
        env_keys=[],
        commit_sha=commit_sha,
        preferred_repo_path=repo_path,
        enable_submodules=False,
        enable_lfs=False,
        force=False,
        mode="light",
        log=lambda _: None,
        llm=dummy,
        report_store=store,
    )
    assert outcome.cache_hit is True
    assert dummy.called is False
    assert dummy.business_called is False


def test_signals_scrub_env_values(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("OPENAI_API_KEY=sk-test", encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=sk-test", encoding="utf-8")

    signals = extract_signals(tmp_path, env_keys=[])
    assert "OPENAI_API_KEY" in signals.get("env_keys", [])
    raw = json.dumps(signals)
    assert "sk-test" not in raw


def test_api_response_does_not_include_env_values() -> None:
    data = {
        "status": "RUNNING",
        "stage": "run",
        "env": {"OPENAI_API_KEY": "sk-test"},
        "env_keys": ["OPENAI_API_KEY"],
    }
    response = build_case_response("c_demo", data).model_dump()
    assert "env" not in response
    assert "sk-test" not in json.dumps(response)


def test_log_redaction_masks_secret_values() -> None:
    entry = decode_log_entry({"line": "OPENAI_API_KEY=sk-test"})
    assert "sk-test" not in entry["line"]
    assert "***" in entry["line"]

    entry = decode_log_entry({"line": "Authorization: Bearer abc.def.ghi"})
    assert "Bearer abc.def.ghi" not in entry["line"]
    assert "Bearer ***" in entry["line"]

    entry = decode_log_entry({"line": "token=eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiZGVtbyJ9.sgn"})
    assert "eyJhbGciOiJIUzI1NiJ9" not in entry["line"]


def test_mermaid_validation_failure_returns_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Demo", encoding="utf-8")

    repo_url = "https://example.com/demo.git"
    commit_sha = "deadbeef"
    store = ReportStore(root=tmp_path / "reports")

    llm = DummyLLM("{\"markdown\":\"# Demo\",\"mermaids\":[\"graph invalid\"]}")

    def always_fail(code, output_dir, index):
        return False, "invalid", None, "syntax"

    monkeypatch.setattr("analyze.service.validate_mermaid", always_fail)

    outcome = run_analysis(
        case_id="c_demo",
        repo_url=repo_url,
        ref=None,
        env_keys=[],
        commit_sha=commit_sha,
        preferred_repo_path=repo_path,
        enable_submodules=False,
        enable_lfs=False,
        force=True,
        mode="light",
        log=lambda _: None,
        llm=llm,
        report_store=store,
    )

    validation = outcome.report.get("validation", {})
    assert validation.get("summary", {}).get("ok") is False
    assert "Mermaid 渲染失败" in outcome.report.get("markdown", "")


def test_analyze_logs_include_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    logs = []

    def fake_publish(case_id, stream, line, level="INFO"):
        logs.append({"stream": stream, "line": line})

    state = {
        "repo_url": "https://example.com/demo.git",
        "env_keys": [],
        "commit_sha": "abc123",
    }

    def fake_get_case(case_id):
        return state

    def fake_update_case(case_id, payload):
        state.update(payload)

    def fake_run_analysis(**kwargs):
        return AnalysisOutcome(report={}, cache_hit=False, commit_sha="abc123", signals_path=None)

    monkeypatch.setattr(worker, "get_case", fake_get_case)
    monkeypatch.setattr(worker, "update_case", fake_update_case)
    monkeypatch.setattr(worker, "publish_log", fake_publish)
    monkeypatch.setattr(worker, "run_analysis", fake_run_analysis)
    monkeypatch.setattr(worker, "release_analyze_lock", lambda key: None)

    worker.analyze_case("c_demo")
    assert any(entry["stream"] == "analyze" for entry in logs)


def test_analyze_lock_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    client = TestClient(main.app)
    case_payload = {"repo_url": "https://example.com/demo.git", "commit_sha": "abc123", "report_ready": False}

    monkeypatch.setattr(main, "get_case", lambda case_id: case_payload)
    monkeypatch.setattr(main, "update_case", lambda case_id, payload: None)
    monkeypatch.setattr(main, "acquire_analyze_lock", lambda key, ttl: False)
    monkeypatch.setattr(main.analyze_case, "delay", lambda *args, **kwargs: None)

    response = client.post("/cases/c_demo/analyze", json={"force": False, "mode": "light"})
    assert response.status_code == 200
    body = response.json()
    assert body.get("message") == "already running"


def test_report_response_redacts_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    client = TestClient(main.app)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Demo", encoding="utf-8")

    repo_url = "https://example.com/demo.git"
    commit_sha = "abc123"
    store = ReportStore(root=tmp_path / "reports")
    llm = DummyLLM("{\"markdown\":\"OPENAI_API_KEY=sk-test\",\"mermaids\":[]}")

    run_analysis(
        case_id="c_demo",
        repo_url=repo_url,
        ref=None,
        env_keys=[],
        commit_sha=commit_sha,
        preferred_repo_path=repo_path,
        enable_submodules=False,
        enable_lfs=False,
        force=True,
        mode="light",
        log=lambda _: None,
        llm=llm,
        report_store=store,
    )

    case_payload = {"repo_url": repo_url, "commit_sha": commit_sha}
    monkeypatch.setattr(main, "get_case", lambda case_id: case_payload)
    monkeypatch.setattr(main, "ReportStore", lambda: store)

    response = client.get("/cases/c_demo/report")
    assert response.status_code == 200
    payload = response.json()
    assert "sk-test" not in json.dumps(payload)
