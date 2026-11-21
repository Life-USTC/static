import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from main import _run_builders


class BuilderIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_builder_restores_output_and_later_builder_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            shared_output = root / "snapshot.sqlite"
            status_path = root / "build-status.json"
            shared_output.write_text("cached", encoding="utf-8")
            later_builder_ran = False

            async def failing_builder() -> None:
                shared_output.write_text("partial", encoding="utf-8")
                raise RuntimeError("upstream unavailable")

            async def succeeding_builder() -> None:
                nonlocal later_builder_ran
                later_builder_ran = True

            with patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
                results = await _run_builders(
                    [
                        ("curriculum", failing_builder, (shared_output,)),
                        ("young", succeeding_builder, (shared_output,)),
                    ],
                    status_path=status_path,
                )

            status = json.loads(status_path.read_text(encoding="utf-8"))

            self.assertEqual(shared_output.read_text(encoding="utf-8"), "cached")
            self.assertTrue(later_builder_ran)
            self.assertEqual(results["curriculum"]["status"], "failed")
            self.assertEqual(results["young"]["status"], "ok")
            self.assertEqual(status["builders"], results)

    async def test_anonymous_catalog_collectors_share_one_session(self):
        sessions = []

        async def collect(session, store):
            sessions.append(session)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
            patch.object(main, "__file__", str(Path(directory) / "main.py")),
            patch.object(main, "load_dotenv"),
            patch("sys.argv", ["main.py", "--catalog-relationships"]),
            patch.object(main, "refresh_catalog_groups", collect),
            patch.object(main, "refresh_study_plans", collect),
            patch.object(main, "export_upstream_schemas", return_value=[]),
            patch.object(
                main, "USTCSession", side_effect=AssertionError("unexpected login")
            ),
        ):
            await main._main()
        self.assertEqual(len(sessions), 2)
        self.assertIs(sessions[0], sessions[1])
        self.assertTrue(sessions[0].client.is_closed)
