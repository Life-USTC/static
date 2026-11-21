import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.sqlite_store import SNAPSHOT_FILENAME, SQLiteModelStore
from tools.platform_snapshots import PLATFORMS, extract, merge


class PlatformSnapshotSmokeTest(unittest.TestCase):
    def test_independent_platforms_merge_without_fetch_id_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for platform in PLATFORMS:
                build = root / platform
                build.mkdir()
                store = SQLiteModelStore(build / SNAPSHOT_FILENAME)
                for prefix in ("catalog", "young", "blackboard"):
                    source = prefix + "_rows"
                    store.conn.execute(
                        f"CREATE TABLE {source} (id INTEGER PRIMARY KEY, "
                        "fetch_id INTEGER REFERENCES upstream_fetches(id), value TEXT)"
                    )
                    # Each worker retains old data for the other platforms.
                    receipt = store.record_fetch(
                        source=source, method="GET", url="https://example.test"
                    )
                    value = (
                        platform
                        if prefix == {"academic": "catalog"}.get(platform, platform)
                        else "stale"
                    )
                    store.conn.execute(
                        f"INSERT INTO {source} VALUES (1, ?, ?)", (receipt, value)
                    )
                store.close()
                (build / "build-status.json").write_text(
                    json.dumps({"builders": {platform: {"status": "ok"}}})
                )
                extract(build, root / "parts" / f"static-{platform}", platform)
            merge(root / "parts", root / "merged")
            with closing(sqlite3.connect(root / "merged" / SNAPSHOT_FILENAME)) as db:
                for prefix, expected in (
                    ("catalog", "academic"),
                    ("young", "young"),
                    ("blackboard", "blackboard"),
                ):
                    self.assertEqual(
                        db.execute(
                            f"SELECT f.source, r.value FROM {prefix}_rows r "
                            "JOIN upstream_fetches f ON f.id=r.fetch_id"
                        ).fetchall(),
                        [(prefix + "_rows", expected)],
                    )
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM upstream_fetches").fetchone(), (3,)
                )
            self.assertEqual(
                set(
                    json.loads((root / "merged/build-status.json").read_text())[
                        "builders"
                    ]
                ),
                set(PLATFORMS),
            )
