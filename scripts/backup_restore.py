#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import ParseResult, quote, unquote, urlparse

from config import DATABASE_URL


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _normalize_db_url(database_url: str | None) -> str:
    value = str(database_url or "").strip()
    if not value:
        raise ValueError("database_url is required")
    return value


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite:///")


def _is_postgres_url(database_url: str) -> bool:
    normalized = database_url.lower()
    return normalized.startswith("postgresql://") or normalized.startswith("postgresql+")


def _sqlite_path_from_url(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("not a sqlite URL")
    raw_path = database_url[len(prefix):].split("?", 1)[0]
    if not raw_path:
        raise ValueError("sqlite path is empty")
    path = Path(unquote(raw_path)).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def _pg_parts(database_url: str) -> tuple[ParseResult, str, str]:
    parsed = urlparse(database_url)
    if not parsed.scheme:
        raise ValueError("invalid postgres database URL")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("postgres host is missing")
    username = unquote(parsed.username or "")
    if not username:
        raise ValueError("postgres username is missing")
    password = unquote(parsed.password or "")
    port = parsed.port or 5432
    path = parsed.path.lstrip("/")
    if not path:
        raise ValueError("postgres database name is missing")
    netloc = f"{quote(username)}@{host}:{port}"
    sanitized = parsed._replace(netloc=netloc).geturl()
    return parsed, password and str(password) or "", sanitized


def backup_sqlite_database(database_url: str, output_dir: Path) -> Path:
    source = _sqlite_path_from_url(database_url)
    if not source.exists():
        raise FileNotFoundError(f"sqlite database not found: {source}")
    output_dir.mkdir(parents=True, exist_ok=True)
    backup_path = output_dir / f"sqlite-backup-{_utc_ts()}.db"

    with sqlite3.connect(str(source)) as src_conn:
        with sqlite3.connect(str(backup_path)) as dst_conn:
            src_conn.backup(dst_conn)
    return backup_path


def restore_sqlite_database(database_url: str, backup_file: Path) -> Path:
    if not backup_file.exists():
        raise FileNotFoundError(f"backup file not found: {backup_file}")
    target = _sqlite_path_from_url(database_url)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(backup_file)) as src_conn:
        with sqlite3.connect(str(target)) as dst_conn:
            src_conn.backup(dst_conn)
    return target


def backup_postgres_database(database_url: str, output_dir: Path) -> Path:
    _, password, sanitized_url = _pg_parts(database_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    backup_path = output_dir / f"postgres-backup-{_utc_ts()}.dump"

    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password

    cmd = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(backup_path),
        sanitized_url,
    ]
    subprocess.run(cmd, env=env, check=True)
    return backup_path


def restore_postgres_database(database_url: str, backup_file: Path, *, force: bool = False) -> None:
    if not backup_file.exists():
        raise FileNotFoundError(f"backup file not found: {backup_file}")
    if not force:
        raise ValueError("restore_postgres_database requires force=True")

    _, password, sanitized_url = _pg_parts(database_url)
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password
    cmd = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        sanitized_url,
        str(backup_file),
    ]
    subprocess.run(cmd, env=env, check=True)


def backup_database(database_url: str, output_dir: Path) -> Path:
    normalized = _normalize_db_url(database_url)
    if _is_sqlite_url(normalized):
        return backup_sqlite_database(normalized, output_dir)
    if _is_postgres_url(normalized):
        return backup_postgres_database(normalized, output_dir)
    raise ValueError("unsupported DATABASE_URL scheme for backup")


def restore_database(database_url: str, backup_file: Path, *, force: bool = False) -> None:
    normalized = _normalize_db_url(database_url)
    if _is_sqlite_url(normalized):
        restore_sqlite_database(normalized, backup_file)
        return
    if _is_postgres_url(normalized):
        restore_postgres_database(normalized, backup_file, force=force)
        return
    raise ValueError("unsupported DATABASE_URL scheme for restore")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AntiHub database backup and restore utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create a database backup")
    backup_parser.add_argument("--database-url", default=DATABASE_URL, help="Target DATABASE_URL")
    backup_parser.add_argument("--output-dir", default="./backups", help="Backup output directory")

    restore_parser = subparsers.add_parser("restore", help="Restore a database backup")
    restore_parser.add_argument("--database-url", default=DATABASE_URL, help="Target DATABASE_URL")
    restore_parser.add_argument("--input", required=True, help="Backup file path")
    restore_parser.add_argument(
        "--force",
        action="store_true",
        help="Required for PostgreSQL restore to avoid accidental destructive restore",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    database_url = _normalize_db_url(args.database_url)
    if args.command == "backup":
        backup_file = backup_database(database_url, Path(args.output_dir))
        print(f"[ok] backup created: {backup_file}")
        return 0
    if args.command == "restore":
        restore_database(
            database_url,
            Path(args.input),
            force=bool(getattr(args, "force", False)),
        )
        print("[ok] restore completed")
        return 0
    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
