import sqlite3
from contextlib import closing
from pathlib import Path

from tools.verify_build import CATALOG_OUTPUTS


def seed_catalog_cache(snapshot: Path) -> None:
    with closing(sqlite3.connect(snapshot)) as db, db:
        for suffixes in CATALOG_OUTPUTS.values():
            for suffix in suffixes:
                table = f"catalog_teach_{suffix}"
                db.execute(f"CREATE TABLE {table} (id INTEGER)")
                db.execute(f"INSERT INTO {table} VALUES (1)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
        )
        db.execute("CREATE TABLE catalog_teach_lesson_infos (semester_id TEXT)")
        db.execute("INSERT INTO catalog_teach_lesson_infos VALUES ('1')")
        db.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("catalog_semester_details_1", '{"lesson_profile_rows":1}'),
        )
