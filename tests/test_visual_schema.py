from pathlib import Path

from analyze.signals import sanitize_text
from evidence import make_evidence, validate_evidence
from visualize.pack import build_storyboard, select_spotlights
from visualize.store import visual_cache_key


def test_storyboard_schema_and_spans() -> None:
    repo_index = {
        "repo_name": "demo",
        "ports": [3000],
        "tree": {"entries": ["src/"]},
        "dependencies": {"python": [{"name": "requests", "source": "requirements.txt"}], "node": []},
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
        "config_files": ["docker-compose.yml"],
    }
    repo_graph = {"nodes": [], "edges": [], "meta": {}}
    spotlights = {
        "items": [
            {
                "file_path": "main.py",
                "snippet": "def main():\n    return 1\n",
                "start_line": 1,
                "end_line": 2,
                "line_range": {"start": 1, "end": 2},
                "highlights": [{"start_line": 1, "end_line": 1}],
                "evidence": make_evidence(
                    "code",
                    [{"kind": "file", "file": "main.py", "line_range": {"start": 1, "end": 2}}],
                    "spotlight_snippet",
                    "strong",
                ),
            }
        ]
    }
    storyboard = build_storyboard(repo_index, repo_graph, spotlights, template_version="v1")
    assert "scenes" in storyboard
    catalog = storyboard.get("evidence_catalog") or []
    evidence_ids = {item.get("id") for item in catalog if validate_evidence(item)}
    for scene in storyboard["scenes"]:
        assert isinstance(scene.get("shots"), list)
        for shot in scene["shots"]:
            assert shot["t_start"] >= 0
            assert shot["t_end"] > shot["t_start"]
            assert "type" in shot
            assert "ref" in shot
            assert shot.get("evidence_id") in evidence_ids
            assert shot.get("evidence_required") is True


def test_spotlights_redaction_and_spans(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "main.py").write_text("API_KEY=sk-secret-123\nprint('ok')", encoding="utf-8")

    spotlights = select_spotlights(repo_path, max_files=1, max_chars=200)
    item = spotlights["items"][0]
    assert "sk-secret" not in item["snippet"]
    assert item["start_line"] == 1
    assert item["end_line"] >= item["start_line"]
    assert item.get("line_range")
    assert validate_evidence(item.get("evidence") or {})


def test_cache_key_includes_template_version() -> None:
    key = visual_cache_key("https://github.com/acme/demo", "abc", template_version="v2")
    assert key.endswith(":v2")


def test_sanitize_text_removes_tokens() -> None:
    text = "Authorization: Bearer abcdef\nOPENAI_API_KEY=sk-test-999"
    cleaned = sanitize_text(text)
    assert "sk-test" not in cleaned
    assert "Bearer ***" in cleaned
