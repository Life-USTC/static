import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.verify_build import (
    BUILDER_NAMES,
    BuildVerificationError,
    _verify_section_counts,
    _write_status_summary,
    verify_build,
)


class BuildVerificationTest(unittest.TestCase):
    def test_section_coverage_matches_each_semester(self) -> None:
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE metadata(key TEXT, value TEXT);
            CREATE TABLE catalog_teach_lesson_infos(semester_id TEXT);
            INSERT INTO metadata VALUES
                ('catalog_semester_details_1','{"lesson_profile_rows":1}');
            INSERT INTO catalog_teach_lesson_infos VALUES ('1');
        """)
        _verify_section_counts(db)
        for sql in (
            "INSERT INTO catalog_teach_lesson_infos VALUES ('2')",
            "DELETE FROM catalog_teach_lesson_infos",
        ):
            db.execute(sql)
            with self.assertRaises(BuildVerificationError):
                _verify_section_counts(db)

    def test_non_curriculum_failure_does_not_claim_contracts_were_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            summary_path = Path(temporary_dir) / "summary.md"
            _write_status_summary(
                summary_path,
                {
                    name: {"status": "ok" if name == "curriculum" else "failed"}
                    for name in BUILDER_NAMES
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
                            name: {"status": "ok" if name == "rss" else "failed"}
                            for name in BUILDER_NAMES
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
