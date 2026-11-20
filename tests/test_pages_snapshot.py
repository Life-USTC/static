import gzip
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.pages_snapshot import FULL_SNAPSHOT_FILENAME, SNAPSHOT_FILENAME, stage


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
                    (1,'jw'),(2,'catalog_teach_course_infos');
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO metadata VALUES
                    ('legacy','kept'),('catalog_semester_details_461','{}');
                CREATE TABLE jw_lessons(
                    fetch_id INTEGER REFERENCES upstream_fetches(id));
                INSERT INTO jw_lessons VALUES(1);
                CREATE TABLE catalog_teach_course_infos_desc(fetch_id INTEGER);
            """)
        original = raw.read_bytes()
        (build / "contract-report.json").touch()
        stage(build, pages, publish=True)
        archive = pages / FULL_SNAPSHOT_FILENAME
        packed = archive.read_bytes()
        second = root / "second"
        stage(build, second, publish=True)
        self.assertEqual((second / FULL_SNAPSHOT_FILENAME).read_bytes(), packed)
        self.assertEqual(gzip.decompress(packed), original)
        self.assertEqual(raw.read_bytes(), original)
        self.assertFalse((pages / "contract-report.json").exists())
        with closing(sqlite3.connect(pages / SNAPSHOT_FILENAME)) as db:
            self.assertEqual(
                db.execute("SELECT id FROM upstream_fetches").fetchall(), [(1,)]
            )
            self.assertEqual(
                db.execute("SELECT key FROM metadata").fetchall(), [("legacy",)]
            )
            self.assertEqual(
                db.execute("SELECT fetch_id FROM jw_lessons").fetchall(), [(1,)]
            )
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("SELECT * FROM catalog_teach_course_infos_desc")
        for source, target in ((pages, restored), (build, legacy)):
            stage(source, target, publish=False)
            self.assertEqual((target / SNAPSHOT_FILENAME).read_bytes(), original)
        archive.write_bytes(packed[:-4])
        with self.assertRaises(EOFError):
            stage(pages, restored, publish=False)
        self.assertEqual((restored / SNAPSHOT_FILENAME).read_bytes(), original)
