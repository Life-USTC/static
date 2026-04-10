from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
SNAPSHOT_FILENAME = "life-ustc-static.sqlite"


def _iter_snapshot_files(build_dir: Path) -> list[Path]:
    files: list[Path] = []

    cache_root = build_dir / "cache"
    if cache_root.exists():
        files.extend(sorted(cache_root.rglob("*.json")))

    bus_data = build_dir / "bus_data_v3.json"
    if bus_data.exists():
        files.append(bus_data)

    return files


def export_sqlite_snapshot(build_dir: Path, output_path: Path | None = None) -> Path:
    if output_path is None:
        output_path = build_dir / SNAPSHOT_FILENAME

    files = _iter_snapshot_files(build_dir)
    if not files:
        raise FileNotFoundError(
            f"No cache JSON files found under {build_dir}; cannot build snapshot"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    generated_at = datetime.now(UTC).isoformat()
    repo_sha = os.getenv("GITHUB_SHA", "").strip() or None

    with sqlite3.connect(output_path) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX files_path_idx ON files(path)")

        metadata_items = {
            "schema_version": str(SCHEMA_VERSION),
            "generated_at": generated_at,
            "file_count": str(len(files)),
        }
        if repo_sha is not None:
            metadata_items["github_sha"] = repo_sha

        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            metadata_items.items(),
        )

        conn.executemany(
            "INSERT INTO files(path, content, sha256) VALUES(?, ?, ?)",
            (
                (
                    file_path.relative_to(build_dir).as_posix(),
                    file_path.read_text(),
                    hashlib.sha256(file_path.read_bytes()).hexdigest(),
                )
                for file_path in files
            ),
        )

    return output_path
