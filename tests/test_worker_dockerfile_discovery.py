from pathlib import Path

import worker
from dockerfile_discovery import resolve_dockerfile


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# stub\n", encoding="utf-8")


def test_worker_formats_discovery_meta_with_backup(tmp_path: Path) -> None:
    _touch(tmp_path / "Dockerfile")
    _touch(tmp_path / "Dockerfile.original")

    selected, context_dir, _, _, meta = resolve_dockerfile(tmp_path, None, None, 2)
    log_meta = worker._format_dockerfile_discovery_meta(meta, context_dir, tmp_path)

    assert selected == tmp_path / "Dockerfile"
    assert log_meta["selected_dockerfile_path"] == "Dockerfile"
    assert log_meta["backup_candidates_filtered"] == ["Dockerfile.original"]
    assert log_meta["selection_reason"] in {"root_dockerfile", "single_candidate"}
