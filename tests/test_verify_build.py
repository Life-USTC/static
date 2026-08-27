import json
import tempfile
import unittest
from pathlib import Path

from tools.verify_build import _write_status_summary, verify_build


class BuildVerificationTest(unittest.TestCase):
    def test_non_curriculum_failure_does_not_claim_contracts_were_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            summary_path = Path(temporary_dir) / "summary.md"
            _write_status_summary(
                summary_path,
                {
                    "curriculum": {"status": "ok"},
                    "young": {"status": "failed"},
                    "rss": {"status": "ok"},
                },
            )

            self.assertNotIn("skipped", summary_path.read_text(encoding="utf-8"))

    def test_failed_authenticated_builders_skip_missing_contract_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            build_dir = root / "build"
            schema_dir = build_dir / "schemas" / "upstream"
            schema_dir.mkdir(parents=True)
            for index in range(7):
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
                        }
                    }
                ),
                encoding="utf-8",
            )
            (build_dir / "rss").mkdir()
            (build_dir / "rss" / "feed.xml").write_text("<rss />", encoding="utf-8")

            verify_build(
                build_dir=build_dir,
                status_path=status_path,
                diagnostic_dir=root / ".artifacts" / "upstream-contracts",
            )
