"""Assemble independent CD platform results without changing published schema."""

import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from src.sqlite_store import SCHEMA_VERSION, SNAPSHOT_FILENAME, _quote_identifier
from tools.snapshot_health import build_health

PLATFORMS = ("academic", "jw", "young", "blackboard")


def owner(name: str) -> str:
    if name.startswith("jw_catalog_"):
        return "jw"
    return next(
        (
            platform
            for platform in ("young", "blackboard")
            if name.startswith(platform + "_")
        ),
        "academic",
    )


def copy_platform(source: Path, target: Path, platform: str) -> None:
    with closing(sqlite3.connect(target)) as db:
        db.execute("ATTACH DATABASE ? AS incoming", (str(source),))
        db.create_function("owned", 1, lambda name: owner(name) == platform)
        version = db.execute(
            "SELECT value FROM incoming.metadata WHERE key='schema_version'"
        ).fetchone()
        if version != (str(SCHEMA_VERSION),):
            raise ValueError("Incompatible platform snapshot schema")
        with db:
            tables = db.execute(
                "SELECT name, sql FROM incoming.sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for name, sql in tables:
                if name in ("metadata", "upstream_fetches") or owner(name) == platform:
                    db.execute(
                        sql.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)
                    )
            offset = db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM upstream_fetches"
            ).fetchone()[0]
            db.execute(
                "CREATE TEMP TABLE fetch_map AS SELECT id AS old, "
                "? + row_number() OVER (ORDER BY id) AS new "
                "FROM incoming.upstream_fetches WHERE owned(source)",
                (offset,),
            )
            db.execute("CREATE UNIQUE INDEX fetch_map_old ON fetch_map(old)")
            for name, _ in tables:
                if (
                    name not in ("metadata", "upstream_fetches")
                    and owner(name) != platform
                ):
                    continue
                columns = [
                    row[1]
                    for row in db.execute(
                        f"PRAGMA incoming.table_info({_quote_identifier(name)})"
                    )
                ]
                expressions = [
                    f"(SELECT new FROM fetch_map WHERE old={_quote_identifier(column)})"
                    if column == "fetch_id"
                    or (name == "upstream_fetches" and column == "id")
                    else _quote_identifier(column)
                    for column in columns
                ]
                where = (
                    " WHERE owned(source)"
                    if name == "upstream_fetches"
                    else " WHERE owned(key) OR key='schema_version'"
                    if name == "metadata"
                    else ""
                )
                db.execute(
                    f"INSERT OR REPLACE INTO {_quote_identifier(name)} "
                    f"({', '.join(map(_quote_identifier, columns))}) "
                    f"SELECT {', '.join(expressions)} "
                    f"FROM incoming.{_quote_identifier(name)}{where}"
                )
            for name, sql in db.execute(
                "SELECT tbl_name, sql FROM incoming.sqlite_schema "
                "WHERE type='index' AND sql IS NOT NULL"
            ):
                if owner(name) == platform:
                    db.execute(
                        sql.replace(
                            "CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1
                        ).replace(
                            "CREATE UNIQUE INDEX",
                            "CREATE UNIQUE INDEX IF NOT EXISTS",
                            1,
                        )
                    )


def extract(build: Path, output: Path, platform: str) -> None:
    output.mkdir(parents=True)
    snapshot = output / SNAPSHOT_FILENAME
    if platform == "academic":
        shutil.copyfile(build / SNAPSHOT_FILENAME, snapshot)
        for name in ("schemas", "life-ustc-static-guesses.sqlite"):
            source = build / name
            if source.is_dir():
                shutil.copytree(source, output / name)
            elif source.is_file():
                shutil.copyfile(source, output / name)
        diagnostics = Path(".artifacts/upstream-contracts")
        if diagnostics.is_dir():
            shutil.copytree(diagnostics, output / "diagnostics")
    else:
        copy_platform(build / SNAPSHOT_FILENAME, snapshot, platform)
    shutil.copyfile(build / "build-status.json", output / "build-status.json")


def merge(parts: Path, build: Path) -> None:
    build.mkdir(parents=True, exist_ok=True)
    academic = parts / "static-academic"
    snapshot = build / SNAPSHOT_FILENAME
    if snapshot.exists():
        raise ValueError("Assembly requires a fresh output directory")
    shutil.copyfile(academic / SNAPSHOT_FILENAME, snapshot)
    with closing(sqlite3.connect(snapshot)) as db, db:
        for (name,) in db.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            if owner(name) != "academic":
                db.execute(f"DROP TABLE {_quote_identifier(name)}")
        db.create_function("other_platform", 1, lambda name: owner(name) != "academic")
        db.execute("DELETE FROM upstream_fetches WHERE other_platform(source)")
        db.execute("DELETE FROM metadata WHERE other_platform(key)")
    builders = {}
    for platform in PLATFORMS:
        part = parts / f"static-{platform}"
        builders.update(
            json.loads((part / "build-status.json").read_text())["builders"]
        )
        if platform != "academic":
            copy_platform(part / SNAPSHOT_FILENAME, snapshot, platform)
    with closing(sqlite3.connect(snapshot)) as db, db:
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Merged platform snapshot has broken references")
        db.execute(
            "UPDATE metadata SET value=? WHERE key='generated_at'",
            (datetime.now(UTC).isoformat(),),
        )
    for name in ("schemas", "life-ustc-static-guesses.sqlite"):
        source = academic / name
        if source.is_dir():
            shutil.copytree(source, build / name, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copyfile(source, build / name)
    if (academic / "diagnostics").is_dir():
        shutil.copytree(
            academic / "diagnostics",
            ".artifacts/upstream-contracts",
            dirs_exist_ok=True,
        )
    status = {"builders": builders, "health": build_health(builders)}
    (build / "build-status.json").write_text(json.dumps(status, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("extract", "merge"))
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--platform", choices=PLATFORMS)
    args = parser.parse_args()
    if args.command == "extract":
        if args.platform is None:
            parser.error("extract requires --platform")
        extract(args.source, args.target, args.platform)
    else:
        merge(args.source, args.target)
