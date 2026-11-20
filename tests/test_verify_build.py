import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.verify_build import (
    BuildVerificationError,
    _verify_section_catalog,
    _write_status_summary,
    verify_build,
)


class BuildVerificationTest(unittest.TestCase):
    def test_section_coverage_matches_each_semester(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.sqlite"
            db = sqlite3.connect(snapshot)
            self.addCleanup(db.close)
            db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE catalog_teach_lesson_infos(semester_id TEXT);
                INSERT INTO metadata VALUES
                    ('catalog_semester_details_1', '{"lesson_profile_rows":1}'),
                    ('catalog_semester_details_2', '{"lesson_profile_rows":0}');
                INSERT INTO catalog_teach_lesson_infos VALUES ('1');
            """)
            _verify_section_catalog(snapshot)
            with db:
                db.execute("INSERT INTO catalog_teach_lesson_infos VALUES ('3')")
            with self.assertRaisesRegex(BuildVerificationError, "lack semester"):
                _verify_section_catalog(snapshot)
            with db:
                db.execute("DELETE FROM catalog_teach_lesson_infos")
            with self.assertRaisesRegex(BuildVerificationError, "semester 1"):
                _verify_section_catalog(snapshot)
            with db:
                db.execute("UPDATE metadata SET value='invalid'")
            with self.assertRaisesRegex(BuildVerificationError, "Invalid section"):
                _verify_section_catalog(snapshot)

    def test_non_curriculum_failure_does_not_claim_contracts_were_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            summary_path = Path(temporary_dir) / "summary.md"
            _write_status_summary(
                summary_path,
                {
                    "curriculum": {"status": "ok"},
                    "young": {"status": "failed"},
                    "rss": {"status": "ok"},
                    "course_catalog": {"status": "failed"},
                    "course_substitutions": {"status": "failed"},
                    "section_catalog": {"status": "failed"},
                    "catalog_groups": {"status": "failed"},
                    "study_plans": {"status": "failed"},
                    "blackboard": {"status": "ok"},
                },
            )

            self.assertNotIn("skipped", summary_path.read_text(encoding="utf-8"))

    def test_failed_authenticated_builders_skip_missing_contract_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            build_dir = root / "build"
            schema_dir = build_dir / "schemas" / "upstream"
            schema_dir.mkdir(parents=True)
            for index in range(11):
                (schema_dir / f"schema-{index}.expected.schema.json").write_text(
                    "{}", encoding="utf-8"
                )
            status_path = build_dir / "build-status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "builders": {
                            "curriculum": {"status": "failed", "error": "Error"},
                            "young": {"status": "failed", "error": "Error"},
                            "rss": {"status": "ok"},
                            "course_catalog": {"status": "failed"},
                            "course_substitutions": {"status": "failed"},
                            "section_catalog": {"status": "failed"},
                            "catalog_groups": {"status": "failed"},
                            "study_plans": {"status": "failed"},
                            "blackboard": {"status": "failed", "error": "Error"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (build_dir / "rss").mkdir()
            (build_dir / "rss" / "feed.xml").write_text("<rss />", encoding="utf-8")
            (build_dir / "imgs" / "rooms").mkdir(parents=True)
            (build_dir / "imgs" / "rooms" / "3A201.png").write_bytes(b"png")
            (build_dir / "room_maps.json").write_text(
                json.dumps(
                    {
                        "rooms": [
                            {
                                "code": "3A201",
                                "building": "三教主",
                                "floor": "2",
                                "imagePath": "imgs/rooms/3A201.png",
                                "sourceImagePath": "imgs/三教主_02.png",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            verify_build(
                build_dir=build_dir,
                status_path=status_path,
                diagnostic_dir=root / ".artifacts" / "upstream-contracts",
            )
