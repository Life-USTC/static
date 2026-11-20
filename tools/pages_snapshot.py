"""Prepare a bounded Pages artifact and restore its complete build cache."""

from __future__ import annotations

import argparse
import gzip
import re
import shutil
import sqlite3
from contextlib import closing
from graphlib import TopologicalSorter
from pathlib import Path
from tempfile import TemporaryDirectory

from src.catalog_groups import (
    CATALOG_GROUP_ENDPOINTS,
    CATALOG_GROUP_ENTRIES,
    CATALOG_GROUP_MEMBERS,
    CATALOG_GROUPS,
)
from src.catalog_semester_details import (
    CATALOG_PUBLIC_LESSONS,
    CATALOG_PUBLIC_SEMESTERS,
)
from src.sqlite_store import SNAPSHOT_FILENAME
from src.study_plans import (
    COURSE_MODULE_TABLE,
    PROGRAM_COURSES_TABLE,
    PROGRAM_INFO_TABLE,
    PROGRAM_TREE_TABLE,
)
from src.upstream_contracts import (
    CATALOG_COURSE_INFOS,
    CATALOG_COURSE_SEARCH,
    CATALOG_COURSE_SUBSTITUTES,
    CATALOG_LESSON_INFOS,
)

FULL_SNAPSHOT_FILENAME = f"{SNAPSHOT_FILENAME}.gz"
PAGES_MAX_BYTES = 1_000_000_000
COPY_BUFFER_BYTES = 1024 * 1024
SIDE_CAR_SUFFIXES = ("-journal", "-wal", "-shm")

# Keep this list scoped to the new public datasets. The existing curriculum,
# school exams, Young, and Blackboard cache belong in the compatibility DB.
PUBLIC_FETCH_SOURCES = (
    CATALOG_COURSE_SEARCH,
    CATALOG_COURSE_INFOS,
    CATALOG_COURSE_SUBSTITUTES,
    CATALOG_PUBLIC_SEMESTERS,
    CATALOG_PUBLIC_LESSONS,
    CATALOG_LESSON_INFOS,
    CATALOG_GROUPS,
    PROGRAM_TREE_TABLE,
    PROGRAM_INFO_TABLE,
    COURSE_MODULE_TABLE,
)
PUBLIC_TABLES = frozenset(
    (
        *PUBLIC_FETCH_SOURCES,
        CATALOG_GROUP_ENTRIES,
        CATALOG_GROUP_ENDPOINTS,
        CATALOG_GROUP_MEMBERS,
        PROGRAM_COURSES_TABLE,
    )
)
PUBLIC_NESTED_ROOTS = (
    CATALOG_COURSE_INFOS,
    CATALOG_COURSE_SUBSTITUTES,
    CATALOG_PUBLIC_LESSONS,
    CATALOG_LESSON_INFOS,
)
PUBLIC_METADATA_KEYS = {
    *(f"{table}_count" for table in PUBLIC_TABLES),
    f"{CATALOG_COURSE_INFOS}_absent_count",
    f"{CATALOG_COURSE_INFOS}_absent",
    f"{CATALOG_GROUPS}_registry_source",
}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _school_catalog_table(name: str) -> bool:
    return name in PUBLIC_TABLES or any(
        name.startswith(table + "_") for table in PUBLIC_NESTED_ROOTS
    )


def _ensure_disjoint(source: Path, target: Path) -> None:
    source, target = source.resolve(), target.resolve()
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Source and destination must use separate directories")


def _assert_no_sidecars(path: Path) -> None:
    if any(path.with_name(path.name + suffix).exists() for suffix in SIDE_CAR_SUFFIXES):
        raise ValueError(f"Close SQLite writers before copying {path}")


def _copy_database(source: Path, target: Path) -> None:
    _assert_no_sidecars(source)
    before = source.stat()
    shutil.copyfile(source, target)
    after = source.stat()
    _assert_no_sidecars(source)
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ValueError(f"SQLite snapshot changed during packaging: {source}")


def _verify_database(path: Path) -> None:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError(f"SQLite snapshot integrity check failed: {path}")
        for table in ("metadata", "upstream_fetches"):
            db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()


def _drop_public_catalog(db: sqlite3.Connection) -> None:
    tables = [
        name
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    removed = {table for table in tables if _school_catalog_table(table)}
    retained = sorted(set(tables) - removed)
    parents = {
        table: {
            relation[2]
            for relation in db.execute(f"PRAGMA foreign_key_list({_quote(table)})")
            if relation[2] in removed and relation[2] != table
        }
        for table in tables
    }

    placeholders = ", ".join("?" for _ in PUBLIC_FETCH_SOURCES)
    new_fetches = f"SELECT id FROM upstream_fetches WHERE source IN ({placeholders})"
    for table in retained:
        if parents[table]:
            raise ValueError(f"Legacy table {table} depends on a public catalog table")
        quoted = _quote(table)
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({quoted})")}
        if (
            "fetch_id" in columns
            and db.execute(
                f"SELECT 1 FROM {quoted} WHERE fetch_id IN ({new_fetches}) LIMIT 1",
                PUBLIC_FETCH_SOURCES,
            ).fetchone()
        ):
            raise ValueError(f"Legacy table {table} uses public catalog provenance")

    order = TopologicalSorter(
        {table: parents[table] for table in removed}
    ).static_order()
    for table in reversed(list(order)):
        db.execute(f"DROP TABLE {_quote(table)}")
    db.execute(
        f"DELETE FROM upstream_fetches WHERE source IN ({placeholders})",
        PUBLIC_FETCH_SOURCES,
    )
    public_keys = [
        (key,)
        for (key,) in db.execute("SELECT key FROM metadata")
        if key in PUBLIC_METADATA_KEYS
        or (
            isinstance(key, str)
            and re.fullmatch(r"catalog_semester_details_[0-9]+", key)
        )
    ]
    db.executemany("DELETE FROM metadata WHERE key = ?", public_keys)


def _compact_compatibility_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            db.execute("BEGIN")
            _drop_public_catalog(db)
        db.execute("VACUUM")
    _verify_database(path)


def _copy_artifact_files(source: Path, target: Path, *, for_pages: bool) -> None:
    def ignored(directory: str, names: list[str]) -> set[str]:
        skipped = set()
        if Path(directory) == source:
            skipped.update((SNAPSHOT_FILENAME, FULL_SNAPSHOT_FILENAME))
            skipped.update(SNAPSHOT_FILENAME + suffix for suffix in SIDE_CAR_SUFFIXES)
        if for_pages:
            skipped.update(
                name
                for name in names
                if name.startswith(".") or name.endswith(".observed.schema.json")
            )
            skipped.add("contract-report.json")
        return skipped

    shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignored)


def _gzip_snapshot(source: Path, target: Path) -> None:
    with (
        source.open("rb") as raw,
        target.open("wb") as output,
        gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as archive,
    ):
        shutil.copyfileobj(raw, archive, COPY_BUFFER_BYTES)


def prepare_pages(build_dir: Path, pages_dir: Path) -> dict[str, int]:
    _ensure_disjoint(build_dir, pages_dir)
    build_dir, pages_dir = build_dir.resolve(), pages_dir.resolve()
    pages_dir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".pages-", dir=pages_dir.parent) as scratch:
        staged = Path(scratch) / "artifact"
        _copy_artifact_files(build_dir, staged, for_pages=True)
        full_copy = staged / SNAPSHOT_FILENAME
        _copy_database(build_dir / SNAPSHOT_FILENAME, full_copy)
        compressed = staged / FULL_SNAPSHOT_FILENAME
        _gzip_snapshot(full_copy, compressed)
        _compact_compatibility_database(full_copy)
        (staged / ".nojekyll").touch()
        sizes = {
            "compatibility": full_copy.stat().st_size,
            "compressed_full": compressed.stat().st_size,
            "pages_total": sum(
                path.stat().st_size for path in staged.rglob("*") if path.is_file()
            ),
        }
        if sizes["pages_total"] > PAGES_MAX_BYTES:
            raise ValueError(
                f"Pages artifact uses {sizes['pages_total']:,} bytes, "
                f"above the {PAGES_MAX_BYTES:,}-byte site limit"
            )
        staged.rename(pages_dir)
        return sizes


def restore_cache(artifact_dir: Path, build_dir: Path) -> str:
    _ensure_disjoint(artifact_dir, build_dir)
    artifact_dir, build_dir = artifact_dir.resolve(), build_dir.resolve()
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".pages-restore-", dir=build_dir.parent) as scratch:
        staged = Path(scratch) / "cache"
        staged.mkdir()
        restored_snapshot = staged / SNAPSHOT_FILENAME
        compressed = artifact_dir / FULL_SNAPSHOT_FILENAME
        if compressed.exists():
            # A bad gzip is grounds to try an earlier artifact. The raw file in
            # this artifact is a compatibility projection, not the full cache.
            with (
                gzip.open(compressed, "rb") as archive,
                restored_snapshot.open("wb") as out,
            ):
                shutil.copyfileobj(archive, out, COPY_BUFFER_BYTES)
            source_name = FULL_SNAPSHOT_FILENAME
        else:
            _copy_database(artifact_dir / SNAPSHOT_FILENAME, restored_snapshot)
            source_name = SNAPSHOT_FILENAME
        _verify_database(restored_snapshot)
        _copy_artifact_files(artifact_dir, staged, for_pages=False)
        staged.rename(build_dir)
        return source_name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("build", type=Path)
    prepare.add_argument("pages", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("artifact", type=Path)
    restore.add_argument("build", type=Path)
    arguments = parser.parse_args()

    if arguments.command == "prepare":
        sizes = prepare_pages(arguments.build, arguments.pages)
        print(
            f"Pages: {sizes['compatibility']:,} bytes compatibility SQLite, "
            f"{sizes['compressed_full']:,} bytes full gzip, "
            f"{sizes['pages_total']:,} bytes total"
        )
    else:
        source = restore_cache(arguments.artifact, arguments.build)
        print(f"Restored complete build cache from {source}")


if __name__ == "__main__":
    main()
