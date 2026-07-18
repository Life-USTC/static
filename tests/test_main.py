import json
import tempfile
import unittest
from pathlib import Path

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
