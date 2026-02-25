from pathlib import Path

from dockerfile_discovery import resolve_dockerfile


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# stub\n", encoding="utf-8")


def test_primary_over_backup(tmp_path: Path) -> None:
    _touch(tmp_path / "Dockerfile")
    _touch(tmp_path / "Dockerfile.original")

    selected, context_dir, _, used_auto, meta = resolve_dockerfile(tmp_path, None, None, 2)

    assert used_auto is True
    assert selected == tmp_path / "Dockerfile"
    assert context_dir == tmp_path
    assert meta["selected_dockerfile"] == "Dockerfile"
    assert meta["selected_backup"] is False
    assert meta["ignored_backups"] == ["Dockerfile.original"]


def test_backup_only_fallback(tmp_path: Path) -> None:
    _touch(tmp_path / "Dockerfile.old")

    selected, _, _, used_auto, meta = resolve_dockerfile(tmp_path, None, None, 2)

    assert used_auto is True
    assert selected == tmp_path / "Dockerfile.old"
    assert meta["selected_dockerfile"] == "Dockerfile.old"
    assert meta["selected_backup"] is True
    assert meta["primary_candidates"] == []
    assert meta["backup_candidates"] == ["Dockerfile.old"]


def test_root_dockerfile_preferred_over_subdir(tmp_path: Path) -> None:
    _touch(tmp_path / "Dockerfile")
    _touch(tmp_path / "docker" / "Dockerfile")

    selected, _, _, _, meta = resolve_dockerfile(tmp_path, None, None, 2)

    assert selected == tmp_path / "Dockerfile"
    assert meta["selection_reason"] == "root_dockerfile"
    assert set(meta["primary_candidates"]) == {"Dockerfile", "docker/Dockerfile"}


def test_root_dockerfile_preferred_over_dev(tmp_path: Path) -> None:
    _touch(tmp_path / "Dockerfile")
    _touch(tmp_path / "Dockerfile.dev")

    selected, _, _, _, meta = resolve_dockerfile(tmp_path, None, None, 2)

    assert selected == tmp_path / "Dockerfile"
    assert meta["selection_reason"] == "root_dockerfile"
    assert set(meta["primary_candidates"]) == {"Dockerfile", "Dockerfile.dev"}


def test_backup_suffix_case_insensitive(tmp_path: Path) -> None:
    _touch(tmp_path / "Dockerfile")
    _touch(tmp_path / "Dockerfile.BAK")

    selected, _, _, _, meta = resolve_dockerfile(tmp_path, None, None, 2)

    assert selected == tmp_path / "Dockerfile"
    assert meta["ignored_backups"] == ["Dockerfile.BAK"]
    assert meta["backup_candidates"] == ["Dockerfile.BAK"]


def test_disabled_is_backup_prod_is_primary(tmp_path: Path) -> None:
    _touch(tmp_path / "Dockerfile")
    _touch(tmp_path / "Dockerfile.disabled")
    _touch(tmp_path / "Dockerfile.prod")

    selected, _, _, _, meta = resolve_dockerfile(tmp_path, None, None, 2)

    assert selected == tmp_path / "Dockerfile"
    assert meta["selection_reason"] == "root_dockerfile"
    assert set(meta["primary_candidates"]) == {"Dockerfile", "Dockerfile.prod"}
    assert meta["ignored_backups"] == ["Dockerfile.disabled"]
