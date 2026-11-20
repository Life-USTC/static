"""Publish exact compressed SQLite and an older-client projection."""

import argparse
import gzip
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from src.sqlite_store import SNAPSHOT_FILENAME, _quote_identifier

FULL_SNAPSHOT_FILENAME = SNAPSHOT_FILENAME + ".gz"
PAGES_MAX_BYTES = 1_000_000_000
CATALOG = re.compile(
    r"catalog_teach_(?:course_(?:infos|search|substitute_pool_list|groups?|module_info)"
    r"|lesson_infos|program_(?:tree|info|courses)|public_(?:semester_list|lesson_list_for_teach))"
    r"(?:_|$)"
)
TERM_METADATA = re.compile(r"catalog_semester_details_[0-9]+$")
DIAGNOSTICS = (".*", "*.observed.schema.json", "contract-report.json")


def compact(db: sqlite3.Connection) -> None:
    db.create_function("catalog", 1, lambda name: bool(name and CATALOG.match(name)))
    tables = db.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND catalog(name)"
    ).fetchall()
    for (table,) in tables:
        db.execute(f"DROP TABLE {_quote_identifier(table)}")
    db.execute("DELETE FROM upstream_fetches WHERE catalog(source)")
    for (table,) in db.execute("""
        SELECT s.name FROM sqlite_schema s JOIN pragma_table_info(s.name) c
        ON c.name='fetch_id' WHERE s.type='table'
    """):
        if db.execute(
            f"SELECT 1 FROM {_quote_identifier(table)} WHERE fetch_id NOT IN "
            "(SELECT id FROM upstream_fetches) LIMIT 1"
        ).fetchone():
            raise ValueError(f"{table} contains a missing fetch reference")
    db.create_function(
        "term_metadata", 1, lambda name: bool(name and TERM_METADATA.match(name))
    )
    db.execute("DELETE FROM metadata WHERE catalog(key) OR term_metadata(key)")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Incomplete legacy SQLite relationships")
    db.commit()
    db.execute("VACUUM")


def copy_snapshot(source: Path, snapshot: Path, publish: bool) -> None:
    archive = source / FULL_SNAPSHOT_FILENAME
    if not publish and archive.exists():
        with gzip.open(archive, "rb") as packed, snapshot.open("wb") as output:
            shutil.copyfileobj(packed, output)
        return
    raw = source / SNAPSHOT_FILENAME
    # Pin the rollback-journal database while copying its exact bytes.
    with closing(sqlite3.connect(f"{raw.as_uri()}?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        db.execute("SELECT key FROM metadata LIMIT 1").fetchone()
        if any(
            raw.with_name(raw.name + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ValueError("Close SQLite writers before packaging")
        shutil.copyfile(raw, snapshot)


def stage(source: Path, target: Path, *, publish: bool) -> None:
    source, target = source.resolve(), target.resolve()
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Source and destination must be separate directories")
    with TemporaryDirectory(dir=target.parent) as scratch:
        staged = Path(scratch) / "artifact"
        ignore = [SNAPSHOT_FILENAME, FULL_SNAPSHOT_FILENAME, SNAPSHOT_FILENAME + "-*"]
        if publish:
            ignore.extend(DIAGNOSTICS)
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns(*ignore))
        snapshot = staged / SNAPSHOT_FILENAME
        copy_snapshot(source, snapshot, publish)
        with closing(sqlite3.connect(snapshot)) as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Invalid SQLite snapshot")
            db.execute(
                "SELECT key, id FROM metadata CROSS JOIN upstream_fetches LIMIT 1"
            )
            if publish:
                with (
                    snapshot.open("rb") as raw,
                    (staged / FULL_SNAPSHOT_FILENAME).open("wb") as output,
                    gzip.GzipFile(
                        filename="", fileobj=output, mode="wb", mtime=0
                    ) as archive,
                ):
                    shutil.copyfileobj(raw, archive)
                compact(db)
        if publish:
            (staged / ".nojekyll").touch()
            total = sum(
                path.stat().st_size for path in staged.rglob("*") if path.is_file()
            )
            if total > PAGES_MAX_BYTES:
                raise ValueError(
                    f"Pages artifact above the site limit: {total:,} bytes"
                )
        staged.rename(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    stage(args.source, args.target, publish=args.command == "prepare")
