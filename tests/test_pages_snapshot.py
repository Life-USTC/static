import gzip
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.pages_snapshot import (
    FULL_SNAPSHOT_FILENAME,
    SNAPSHOT_FILENAME,
    prepare_pages,
    restore_cache,
)


class PagesSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = self.enterContext(TemporaryDirectory())
        self.root = Path(scratch)
        self.build = self.root / "build"
        self.build.mkdir()
        self.snapshot = self.build / SNAPSHOT_FILENAME
        self.pages = self.root / "pages"
        self.legacy_rows = [
            (
                "catalog_teach_lesson_list_for_teach",
                "catalog_teach_lesson_list_for_teach",
            ),
            ("catalog_teach_exam_list", "catalog_teach_exam_list"),
            ("jw_ws_schedule_table_datum", "jw_ws_schedule_table_datum"),
            ("young_mobile_item_enrolment_list", "young_mobile_item_enrolment_list"),
            ("young_referenced_organization_names", "young_organization_names"),
            ("blackboard_resources", "blackboard_resource"),
            (
                "catalog_teach_additional_legacy_data",
                "catalog_teach_additional_legacy_data",
            ),
        ]
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            db.executescript("""
                CREATE TABLE upstream_fetches(id INTEGER PRIMARY KEY, source TEXT);
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO metadata VALUES ('legacy_sync_at', 'kept');
                INSERT INTO metadata VALUES
                    ('catalog_semester_details_historical', 'kept');
                INSERT INTO metadata VALUES ('catalog_teach_lesson_infos_count', '1');
                INSERT INTO metadata VALUES ('catalog_teach_course_infos_absent', '[]');
                INSERT INTO metadata VALUES ('catalog_semester_details_461', '{}');
                INSERT INTO upstream_fetches VALUES (101, 'catalog_teach_lesson_infos');
                INSERT INTO upstream_fetches VALUES (102, 'catalog_teach_course_infos');
                INSERT INTO upstream_fetches VALUES
                    (103, 'catalog_teach_course_groups');
                INSERT INTO upstream_fetches VALUES (104, 'catalog_teach_program_info');
                INSERT INTO upstream_fetches VALUES
                    (105, 'catalog_teach_course_module_info');
                CREATE TABLE catalog_teach_lesson_infos(code TEXT, fetch_id INTEGER);
                INSERT INTO catalog_teach_lesson_infos VALUES ('MATH1006.01', 101);
                CREATE TABLE catalog_teach_course_infos(code TEXT, fetch_id INTEGER);
                INSERT INTO catalog_teach_course_infos VALUES ('MATH1006', 102);
                CREATE TABLE catalog_teach_course_infos_syllabus_periods(
                    day TEXT, fetch_id INTEGER
                );
                INSERT INTO catalog_teach_course_infos_syllabus_periods
                    VALUES ('Mon', 102);
                CREATE TABLE catalog_teach_course_group_members(
                    course_code TEXT, fetch_id INTEGER
                );
                INSERT INTO catalog_teach_course_group_members VALUES ('MATH1006', 103);
                CREATE TABLE catalog_teach_program_info(
                    id INTEGER PRIMARY KEY,
                    fetch_id INTEGER REFERENCES upstream_fetches(id)
                );
                INSERT INTO catalog_teach_program_info VALUES (1,104);
                CREATE TABLE catalog_teach_course_module_info(
                    id INTEGER PRIMARY KEY,
                    fetch_id INTEGER REFERENCES upstream_fetches(id)
                );
                INSERT INTO catalog_teach_course_module_info VALUES (1,105);
                CREATE TABLE catalog_teach_program_courses(
                    program_id INTEGER REFERENCES catalog_teach_program_info(id),
                    module_id INTEGER REFERENCES catalog_teach_course_module_info(id)
                );
                INSERT INTO catalog_teach_program_courses VALUES (1,1);
            """)
            for id_, (table, source) in enumerate(self.legacy_rows, start=1):
                db.execute("INSERT INTO upstream_fetches VALUES (?,?)", (id_, source))
                db.execute(
                    f"CREATE TABLE {table}(sample TEXT, "
                    "fetch_id INTEGER REFERENCES upstream_fetches(id))"
                )
                db.execute(f"INSERT INTO {table} VALUES ('original', ?)", (id_,))
        (self.build / "room_maps.json").write_text("{}", encoding="utf-8")
        (self.build / ".ignored-catalog-cache").touch()
        (self.build / "life-ustc-static-guesses.sqlite").write_bytes(b"independent DB")
        schema_dir = self.build / "schemas"
        schema_dir.mkdir()
        (schema_dir / "course.expected.schema.json").touch()
        (schema_dir / "course.observed.schema.json").touch()
        (schema_dir / "contract-report.json").touch()

    def test_gzip_round_trips_and_only_legacy_data_remains_at_old_url(self) -> None:
        original = self.snapshot.read_bytes()
        first = prepare_pages(self.build, self.pages)
        second = self.root / "pages-again"
        self.assertEqual(prepare_pages(self.build, second), first)
        compressed = self.pages / FULL_SNAPSHOT_FILENAME
        self.assertEqual(
            compressed.read_bytes(), (second / compressed.name).read_bytes()
        )
        self.assertEqual(gzip.decompress(compressed.read_bytes()), original)
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertTrue((self.build / "schemas/contract-report.json").exists())
        self.assertTrue((self.pages / ".nojekyll").exists())
        self.assertFalse((self.pages / ".ignored-catalog-cache").exists())
        self.assertTrue((self.pages / "room_maps.json").exists())
        self.assertTrue((self.pages / "schemas/course.expected.schema.json").exists())
        self.assertFalse((self.pages / "schemas/course.observed.schema.json").exists())
        self.assertFalse((self.pages / "schemas/contract-report.json").exists())
        with closing(sqlite3.connect(self.pages / SNAPSHOT_FILENAME)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                db.execute(
                    "SELECT source FROM upstream_fetches ORDER BY id"
                ).fetchall(),
                [(source,) for _, source in self.legacy_rows],
            )
            self.assertEqual(
                db.execute("SELECT key, value FROM metadata ORDER BY key").fetchall(),
                [
                    ("catalog_semester_details_historical", "kept"),
                    ("legacy_sync_at", "kept"),
                ],
            )
            self.assertEqual(
                {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                },
                {
                    "upstream_fetches",
                    "metadata",
                    *(table for table, _ in self.legacy_rows),
                },
            )
            for id_, (table, _) in enumerate(self.legacy_rows, start=1):
                self.assertEqual(
                    db.execute(f"SELECT sample, fetch_id FROM {table}").fetchall(),
                    [("original", id_)],
                )
        restored = self.root / "restored"
        self.assertEqual(restore_cache(self.pages, restored), FULL_SNAPSHOT_FILENAME)
        self.assertEqual((restored / SNAPSHOT_FILENAME).read_bytes(), original)
        self.assertEqual(
            (restored / "life-ustc-static-guesses.sqlite").read_bytes(),
            b"independent DB",
        )
        self.assertFalse((restored / FULL_SNAPSHOT_FILENAME).exists())

    def test_restores_older_raw_only_artifact(self) -> None:
        restored = self.root / "restored"
        restored.mkdir()
        self.assertEqual(restore_cache(self.build, restored), SNAPSHOT_FILENAME)
        self.assertEqual(
            (restored / SNAPSHOT_FILENAME).read_bytes(), self.snapshot.read_bytes()
        )
        self.assertTrue((restored / "room_maps.json").exists())

    def test_corrupt_gzip_does_not_silently_restore_the_compatibility_copy(
        self,
    ) -> None:
        prepare_pages(self.build, self.pages)
        compressed = self.pages / FULL_SNAPSHOT_FILENAME
        compressed.write_bytes(compressed.read_bytes()[:-4])
        restored = self.root / "restored"
        with self.assertRaises(EOFError):
            restore_cache(self.pages, restored)
        self.assertFalse(restored.exists())

    def test_budget_failure_leaves_build_intact(self) -> None:
        original = self.snapshot.read_bytes()
        with (
            patch("tools.pages_snapshot.PAGES_MAX_BYTES", 1),
            self.assertRaisesRegex(ValueError, "above the"),
        ):
            prepare_pages(self.build, self.pages)
        self.assertFalse(self.pages.exists())
        self.assertEqual(self.snapshot.read_bytes(), original)

    def test_existing_data_with_public_provenance_is_never_pruned(self) -> None:
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            db.execute("INSERT INTO blackboard_resources VALUES ('shared', 101)")
        original = self.snapshot.read_bytes()
        with self.assertRaisesRegex(ValueError, "uses public catalog provenance"):
            prepare_pages(self.build, self.pages)
        self.assertFalse(self.pages.exists())
        self.assertEqual(self.snapshot.read_bytes(), original)

    def test_snapshot_with_sqlite_sidecar_is_never_packaged(self) -> None:
        self.snapshot.with_name(self.snapshot.name + "-wal").touch()
        with self.assertRaisesRegex(ValueError, "Close SQLite writers"):
            prepare_pages(self.build, self.pages)
        self.assertFalse(self.pages.exists())
