from pathlib import Path

from strategy_engine import inspect_repo, select_strategy


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_auto_generates_for_node_repo(tmp_path: Path) -> None:
    _touch(tmp_path / "package.json", "{\"name\": \"demo\", \"scripts\": {\"start\": \"node server.js\"}}")
    inspection = inspect_repo(tmp_path, search_depth=1)
    decision = select_strategy("auto", inspection)
    assert inspection.repo_type == "node"
    assert decision.strategy == "generated"


def test_auto_showcase_for_miniprogram(tmp_path: Path) -> None:
    _touch(tmp_path / "app.json", "{}")
    _touch(tmp_path / "project.config.json", "{}")
    inspection = inspect_repo(tmp_path, search_depth=1)
    decision = select_strategy("auto", inspection)
    assert inspection.repo_type == "miniprogram"
    assert decision.strategy == "showcase"


def test_container_requires_dockerfile(tmp_path: Path) -> None:
    _touch(tmp_path / "package.json", "{\"name\": \"demo\"}")
    inspection = inspect_repo(tmp_path, search_depth=1)
    decision = select_strategy("container", inspection)
    assert decision.strategy == "none"
    assert decision.fallback_reason == "dockerfile_not_found"
