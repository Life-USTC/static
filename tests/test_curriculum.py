import unittest
from json import JSONDecodeError
from unittest.mock import AsyncMock, MagicMock, patch

from src.curriculum import (
    _cached_complete_semester_ids,
    _has_cached_jw_schedule,
    _jw_schedule_expected_chunk_count_key,
    _refresh_curriculum_semesters,
    _selected_curriculum_semesters,
    _semester_has_ended,
    _should_fetch_catalog_exams,
    _should_fetch_catalog_lessons,
    _should_fetch_jw_schedule_table,
    _store_jw_schedule_chunks,
)
from src.models.api.catalog_api_teach_lesson_list_for_teach import (
    TeachLessonListResponse,
)
from src.models.semester import Semester
from src.sqlite_store import SQLiteModelStore


def _semester(
    semester_id: str,
    *,
    end_date: int = 0,
) -> Semester:
    return Semester(
        id=semester_id,
        courses=[],
        name=f"semester {semester_id}",
        startDate=0,
        endDate=end_date,
    )


class CatalogLessonFetchTest(unittest.TestCase):
    def test_skips_semesters_below_minimum_lesson_id(self) -> None:
        self.assertFalse(_should_fetch_catalog_lessons("53"))
        self.assertFalse(_should_fetch_catalog_lessons("202"))

    def test_fetches_semesters_at_or_above_minimum_lesson_id(self) -> None:
        self.assertTrue(_should_fetch_catalog_lessons("221"))
        self.assertTrue(_should_fetch_catalog_lessons("381"))

    def test_fetches_non_numeric_semester_ids(self) -> None:
        self.assertTrue(_should_fetch_catalog_lessons("latest"))

    def test_selected_curriculum_semesters_filter_legacy_ids(self) -> None:
        selected = _selected_curriculum_semesters(
            [_semester("202"), _semester("221"), _semester("381")]
        )

        self.assertEqual([semester.id for semester in selected], ["221", "381"])


class JwScheduleFetchTest(unittest.TestCase):
    def test_fetches_schedule_for_any_selected_semester_id(self) -> None:
        self.assertTrue(_should_fetch_jw_schedule_table("2"))
        self.assertTrue(_should_fetch_jw_schedule_table("81"))
        self.assertTrue(_should_fetch_jw_schedule_table("221"))

    def test_fetches_schedule_for_non_numeric_semester_ids(self) -> None:
        self.assertTrue(_should_fetch_jw_schedule_table("latest"))


class JwScheduleChunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_records_expected_count_and_accepts_all_successful_chunks(
        self,
    ) -> None:
        store = SQLiteModelStore(":memory:")
        guesses = MagicMock()
        try:
            catalog_fetch_id = store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/401",
                context={"semester_id": "401"},
            )
            store.conn.execute(
                "CREATE TABLE catalog_teach_lesson_list_for_teach("
                "store_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "fetch_id INTEGER NOT NULL)"
            )
            store.conn.executemany(
                "INSERT INTO catalog_teach_lesson_list_for_teach(fetch_id) VALUES(?)",
                [(catalog_fetch_id,)] * 101,
            )
            with patch(
                "src.curriculum.fetch_jw_schedule_table_json",
                new_callable=AsyncMock,
                return_value={"result": None},
            ):
                await _store_jw_schedule_chunks(
                    session=MagicMock(),
                    store=store,
                    guesses=guesses,
                    semester_id="401",
                    catalog_response=TeachLessonListResponse(root=[]),
                    courses=[MagicMock() for _ in range(101)],
                )

            metadata_key = _jw_schedule_expected_chunk_count_key("401")
            expected_count = store.conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (metadata_key,),
            ).fetchone()
            chunk_size = store.conn.execute(
                "SELECT value FROM metadata WHERE key = 'jw_schedule_chunk_size'"
            ).fetchone()
            complete = _has_cached_jw_schedule(store, "401")
            store.conn.execute("DELETE FROM metadata WHERE key = ?", (metadata_key,))
            legacy_complete = _has_cached_jw_schedule(store, "401")
        finally:
            store.close()

        self.assertEqual(expected_count, ("2",))
        self.assertEqual(chunk_size, ("100",))
        self.assertTrue(complete)
        self.assertTrue(legacy_complete)

    async def test_missing_chunk_is_not_complete(self) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            store.put_metadata({_jw_schedule_expected_chunk_count_key("401"): 2})
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "401", "chunk_index": 0},
            )

            complete = _has_cached_jw_schedule(store, "401")
        finally:
            store.close()

        self.assertFalse(complete)

    async def test_failed_chunk_aborts_refresh_and_is_not_complete(self) -> None:
        store = SQLiteModelStore(":memory:")
        guesses = MagicMock()
        try:
            with (
                patch(
                    "src.curriculum.fetch_jw_schedule_table_json",
                    new_callable=AsyncMock,
                    side_effect=[
                        {"result": None},
                        JSONDecodeError("non-json", "<html>", 0),
                    ],
                ),
                self.assertRaises(JSONDecodeError),
            ):
                await _store_jw_schedule_chunks(
                    session=MagicMock(),
                    store=store,
                    guesses=guesses,
                    semester_id="401",
                    catalog_response=TeachLessonListResponse(root=[]),
                    courses=[MagicMock() for _ in range(101)],
                )

            complete = _has_cached_jw_schedule(store, "401")
        finally:
            store.close()

        self.assertFalse(complete)


class CatalogExamFetchTest(unittest.TestCase):
    def test_skips_semesters_below_minimum_exam_id(self) -> None:
        self.assertFalse(_should_fetch_catalog_exams("221"))
        self.assertFalse(_should_fetch_catalog_exams("362"))

    def test_fetches_semesters_at_or_above_minimum_exam_id(self) -> None:
        self.assertTrue(_should_fetch_catalog_exams("381"))
        self.assertTrue(_should_fetch_catalog_exams("401"))

    def test_fetches_non_numeric_semester_ids(self) -> None:
        self.assertTrue(_should_fetch_catalog_exams("latest"))


class SemesterCacheTest(unittest.TestCase):
    def test_semester_has_ended_only_when_end_date_is_before_now(self) -> None:
        self.assertTrue(_semester_has_ended(_semester("221", end_date=100), 101))
        self.assertFalse(_semester_has_ended(_semester("441", end_date=100), 100))
        self.assertFalse(_semester_has_ended(_semester("latest", end_date=0), 100))

    def test_refreshes_unended_or_missing_cached_semesters(self) -> None:
        semesters = [
            _semester("221", end_date=100),
            _semester("241", end_date=100),
            _semester("441", end_date=300),
            _semester("461", end_date=400),
        ]

        refreshed = _refresh_curriculum_semesters(
            semesters,
            cached_semester_ids={"221", "441"},
            now_timestamp=200,
        )

        self.assertEqual([semester.id for semester in refreshed], ["241", "441", "461"])

    def test_cached_complete_semester_ids_require_lesson_jw_and_exam_when_needed(
        self,
    ) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            lesson_221 = store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/221",
                context={"semester_id": "221"},
            )
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "221", "chunk_index": 0},
            )
            store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/381",
                context={"semester_id": "381"},
            )
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "381", "chunk_index": 0},
            )
            store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/401",
                context={"semester_id": "401"},
            )
            store.record_fetch(
                source="catalog_teach_exam_list",
                method="GET",
                url="exam/401",
                context={"semester_id": "401"},
            )
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "401", "chunk_index": 0},
            )
            store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/421",
                context={"semester_id": "421"},
            )
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "421", "chunk_index": 0},
                ok=False,
                error="non-json",
            )
            for semester_id in ("221", "381", "401", "421"):
                store.put_metadata(
                    {_jw_schedule_expected_chunk_count_key(semester_id): 1}
                )

            cached = _cached_complete_semester_ids(
                store,
                [
                    _semester("221"),
                    _semester("381"),
                    _semester("401"),
                    _semester("421"),
                ],
            )

            self.assertEqual(cached, {"221", "401"})
            self.assertIsInstance(lesson_221, int)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
