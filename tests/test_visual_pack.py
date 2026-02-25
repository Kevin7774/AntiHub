import json
from pathlib import Path

from evidence import make_evidence, validate_evidence
from visualize.pack import (
    build_knowledge_graph,
    build_repo_graph,
    build_repo_index,
    build_storyboard,
    select_spotlights,
)


def test_repo_index_languages_and_ports(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("Service runs on port 8080", encoding="utf-8")
    (repo_path / "main.py").write_text("print('hello')", encoding="utf-8")
    (repo_path / "app.js").write_text("console.log('hi')", encoding="utf-8")
    (repo_path / "requirements.txt").write_text("requests==2.32.0", encoding="utf-8")
    (repo_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18.0.0"}}), encoding="utf-8"
    )

    index = build_repo_index(repo_path, env_keys=[], commit_sha="abc123", template_version="vtest")
    languages = [item["name"] for item in index.get("languages", [])]
    assert "Python" in languages
    assert "JavaScript" in languages
    assert 8080 in (index.get("ports") or [])
    deps = index.get("dependencies") or {}
    python_deps = [item.get("name") for item in deps.get("python") or [] if isinstance(item, dict)]
    node_deps = [item.get("name") for item in deps.get("node") or [] if isinstance(item, dict)]
    assert "requests" in python_deps
    assert "react" in node_deps


def test_repo_index_uses_repo_name_from_url(tmp_path: Path) -> None:
    repo_path = tmp_path / "https-github.com-adenhq-hive"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Hive", encoding="utf-8")
    index = build_repo_index(
        repo_path,
        repo_url="https://github.com/adenhq/hive",
        env_keys=[],
        commit_sha="abc123",
        template_version="vtest",
    )
    assert index.get("repo_name") == "hive"
    assert index.get("repo_slug") == "https-github.com-adenhq-hive"


def test_repo_index_readme_summary_strips_html(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text(
        '<p align="center"><img alt="Hive Banner" src="https://example.com/banner.png" /></p>\n'
        '<a href="README.md">English</a> | <a href="docs/i18n/zh-CN.md">简体中文</a>',
        encoding="utf-8",
    )
    index = build_repo_index(
        repo_path,
        env_keys=[],
        commit_sha="abc123",
        template_version="vtest",
        ingest_meta={
            "readme_rendered": (
                '<p align="center"><img alt="Hive Banner" src="https://example.com/banner.png" /></p>'
                '<a href="README.md">English</a> | <a href="docs/i18n/zh-CN.md">简体中文</a>'
            ),
        },
    )
    text = str((index.get("readme_summary") or {}).get("text") or "")
    assert "<img" not in text
    assert "<a " not in text
    assert "English" in text


def test_spotlights_redact_and_truncate(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    secret_text = "OPENAI_API_KEY=sk-test-secret\n" * 50
    (repo_path / "main.py").write_text(secret_text, encoding="utf-8")

    spotlights = select_spotlights(repo_path, max_files=1, max_chars=120)
    items = spotlights.get("items") or []
    assert items
    snippet = items[0].get("snippet") or ""
    assert "sk-test-secret" not in snippet
    assert len(snippet) <= 120
    assert items[0].get("file_path") == "main.py"
    assert items[0].get("start_line") == 1
    assert items[0].get("end_line") >= items[0].get("start_line")
    assert items[0].get("explanation")
    assert items[0].get("line_range")
    assert validate_evidence(items[0].get("evidence") or {})


def test_storyboard_has_five_scenes() -> None:
    repo_index = {
        "repo_name": "demo",
        "ports": [3000],
        "tree": {"entries": ["src/"]},
        "dependencies": {"python": [{"name": "requests", "source": "requirements.txt"}], "node": []},
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
        "config_files": ["docker-compose.yml"],
    }
    repo_graph = build_repo_graph(repo_index, max_nodes=5)
    spotlights = {
        "items": [
            {
                "file_path": "main.py",
                "snippet": "print('hi')",
                "start_line": 1,
                "end_line": 1,
                "line_range": {"start": 1, "end": 1},
                "evidence": make_evidence(
                    "code",
                    [{"kind": "file", "file": "main.py", "line_range": {"start": 1, "end": 1}}],
                    "spotlight_snippet",
                    "strong",
                ),
            }
        ]
    }
    storyboard = build_storyboard(repo_index, repo_graph, spotlights, template_version="v1")
    scenes = storyboard.get("scenes") or []
    assert len(scenes) == 5
    total = storyboard.get("total_duration")
    assert total == sum(scene.get("duration", 0) for scene in scenes)
    for scene in scenes:
        shots = scene.get("shots") or []
        assert shots
        for shot in shots:
            assert shot.get("t_start", 0) < shot.get("t_end", 0)


def test_storyboard_varies_by_evidence() -> None:
    repo_index_full = {
        "repo_name": "demo",
        "ports": [3000],
        "tree": {"entries": ["src/"]},
        "dependencies": {"python": [{"name": "requests", "source": "requirements.txt"}], "node": []},
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
        "config_files": ["docker-compose.yml"],
    }
    repo_graph_full = build_repo_graph(repo_index_full, max_nodes=5)
    spotlights_full = {
        "items": [
            {
                "file_path": "main.py",
                "snippet": "print('hi')",
                "start_line": 1,
                "end_line": 1,
                "line_range": {"start": 1, "end": 1},
                "evidence": make_evidence(
                    "code",
                    [{"kind": "file", "file": "main.py", "line_range": {"start": 1, "end": 1}}],
                    "spotlight_snippet",
                    "strong",
                ),
            }
        ]
    }
    storyboard_full = build_storyboard(repo_index_full, repo_graph_full, spotlights_full, template_version="v1")

    repo_index_min = {
        "repo_name": "demo",
        "ports": [],
        "tree": {"entries": ["src/"]},
        "dependencies": {"python": [], "node": []},
        "readme_summary": {"text": "Demo", "path": "README.md", "line_range": {"start": 1, "end": 1}},
        "config_files": [],
    }
    repo_graph_min = build_repo_graph(repo_index_min, max_nodes=5)
    storyboard_min = build_storyboard(repo_index_min, repo_graph_min, {"items": []}, template_version="v1")

    ids_full = {scene.get("id") for scene in storyboard_full.get("scenes") or []}
    ids_min = {scene.get("id") for scene in storyboard_min.get("scenes") or []}
    assert ids_full != ids_min
    assert "graph" in ids_full
    assert "graph" not in ids_min


def test_build_knowledge_graph_has_analysis_and_relations() -> None:
    repo_index = {
        "repo_name": "demo",
        "tree": {"entries": ["src/", "  service.py", "README.md"]},
        "dependencies": {
            "python": [{"name": "requests", "source": "requirements.txt"}],
            "node": [{"name": "react", "source": "package.json"}],
        },
        "entrypoints": [{"path": "src/service.py", "kind": "file"}],
        "config_files": ["docker-compose.yml"],
        "readme_summary": {"text": "Demo graph"},
    }
    repo_graph = build_repo_graph(repo_index, max_nodes=12)
    spotlights = {
        "items": [
            {
                "file_path": "src/service.py",
                "language": "Python",
                "snippet": "import requests\nfrom src.service import run",
            }
        ]
    }
    graph = build_knowledge_graph(repo_index, repo_graph, spotlights, max_nodes=64)
    assert (graph.get("nodes") or [])
    assert (graph.get("edges") or [])
    assert isinstance(graph.get("analysis"), dict)
    assert graph["analysis"].get("summary")
    relations = {edge.get("relation") for edge in graph.get("edges") or []}
    assert "包含" in relations or "依赖" in relations
