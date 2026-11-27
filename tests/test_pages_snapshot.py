import gzip
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.pages_snapshot import (
    FULL_SNAPSHOT_FILENAME,
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    stage,
)


class PagesSnapshotTest(unittest.TestCase):
    def test_package_and_restore_current_and_legacy_formats(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        build, pages, restored, legacy = (
            root / name for name in ("build", "pages", "restored", "legacy")
        )
        build.mkdir()
        raw = build / SNAPSHOT_FILENAME
        with closing(sqlite3.connect(raw)) as db:
            db.executescript("""
                CREATE TABLE upstream_fetches(id INTEGER PRIMARY KEY, source TEXT);
                INSERT INTO upstream_fetches VALUES
                    (1,'jw'),(2,'catalog_teach_course_infos'),(3,'jw_catalog_lesson_infos');
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO metadata VALUES
                    ('legacy','kept'),('catalog_semester_details_461','{}'),('jw_catalog_semester_461','{}'),
                    ('schema_version','6'),('generated_at','2026-11-26T00:00:00+00:00'),
                    ('curriculum_unavailable_semester_ids','401,402'),
                    ('catalog_exam_unavailable_semester_ids','402');
                CREATE TABLE jw_lessons(
                    fetch_id INTEGER REFERENCES upstream_fetches(id));
                INSERT INTO jw_lessons VALUES(1);
                CREATE TABLE catalog_teach_course_infos_desc(fetch_id INTEGER);
                CREATE TABLE jw_catalog_lesson_infos(fetch_id INTEGER);
                INSERT INTO jw_catalog_lesson_infos VALUES(3);
            """)
        original = raw.read_bytes()
        (build / "contract-report.json").touch()
        (build / "build-status.json").write_text(
            json.dumps({"builders": {"curriculum": {"status": "ok"}}})
        )
        stage(build, pages, publish=True)
        archive = pages / FULL_SNAPSHOT_FILENAME
        packed = archive.read_bytes()
        manifest = json.loads((pages / MANIFEST_FILENAME).read_text())
        self.assertEqual(manifest["manifestVersion"], 1)
        self.assertEqual(manifest["snapshotSchemaVersion"], "6")
        self.assertEqual(manifest["health"], "complete")
        self.assertEqual(
            manifest["sourceAvailability"],
            {
                "unavailableCurriculumSemesterJwIds": [401, 402],
                "unavailableExamSemesterJwIds": [402],
            },
        )
        self.assertEqual(
            manifest["snapshot"],
            {
                "sha256": hashlib.sha256(original).hexdigest(),
                "size": len(original),
            },
        )
        self.assertEqual(
            manifest["compressedSnapshot"],
            {
                "url": FULL_SNAPSHOT_FILENAME,
                "sha256": hashlib.sha256(packed).hexdigest(),
                "size": len(packed),
            },
        )
        self.assertEqual(
            json.loads((pages / "build-status.json").read_text())["health"],
            "complete",
        )
        second = root / "second"
        stage(build, second, publish=True)
        self.assertEqual((second / FULL_SNAPSHOT_FILENAME).read_bytes(), packed)
        self.assertEqual(gzip.decompress(packed), original)
        self.assertEqual(raw.read_bytes(), original)
        self.assertFalse((pages / "contract-report.json").exists())
        with closing(sqlite3.connect(pages / SNAPSHOT_FILENAME)) as db:
            for query, expected in (
                ("SELECT id FROM upstream_fetches", [(1,)]),
                (
                    "SELECT key FROM metadata ORDER BY key",
                    [
                        ("catalog_exam_unavailable_semester_ids",),
                        ("curriculum_unavailable_semester_ids",),
                        ("generated_at",),
                        ("legacy",),
                        ("schema_version",),
                    ],
                ),
                ("SELECT fetch_id FROM jw_lessons", [(1,)]),
            ):
                self.assertEqual(db.execute(query).fetchall(), expected, query)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("SELECT * FROM catalog_teach_course_infos_desc")
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("SELECT * FROM jw_catalog_lesson_infos")
        for source, target in ((pages, restored), (build, legacy)):
            stage(source, target, publish=False)
            self.assertEqual((target / SNAPSHOT_FILENAME).read_bytes(), original)
        archive.write_bytes(packed[:-4])
        with self.assertRaises(EOFError):
            stage(pages, restored, publish=False)
        self.assertEqual((restored / SNAPSHOT_FILENAME).read_bytes(), original)
