import unittest
from json import JSONDecodeError
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.curriculum import (
    _cached_complete_semester_ids,
    _course_ids_by_code_from_response,
    _has_cached_jw_schedule,
    _is_skippable_exam_fetch_error,
    _jw_schedule_expected_chunk_count_key,
    _refresh_curriculum_semesters,
    _selected_curriculum_semesters,
    _semester_has_ended,
    _should_fetch_catalog_exams,
    _should_fetch_catalog_lessons,
    _should_fetch_jw_schedule_table,
    _store_jw_schedule_chunks,
    _stored_course_ids_by_code,
)
from src.models.api.catalog_api_teach_lesson_list_for_teach import (
    Course,
    TeachLessonListItem,
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


def _catalog_lesson(
    lesson_id: int,
    *,
    course_id: int | None,
    course_code: str | None,
    course_cn: str | None = "课程",
) -> TeachLessonListItem:
    values = {name: None for name in TeachLessonListItem.model_fields}
    values["id"] = lesson_id
    if course_id is not None or course_code is not None:
        course_values = {name: None for name in Course.model_fields}
        course_values.update(id=course_id, code=course_code, cn=course_cn)
        values["course"] = Course(**course_values)
    return TeachLessonListItem(**values)


class CatalogCourseIdentityTest(unittest.TestCase):
    def test_preserves_distinct_lesson_and_course_ids(self) -> None:
        response = TeachLessonListResponse(
            root=[_catalog_lesson(181384, course_id=144481, course_code="MATH1001")]
        )
        store = SQLiteModelStore(":memory:")
        try:
            store.register_response_model(
                table_name="catalog_teach_lesson_list_for_teach",
                response_model=TeachLessonListResponse,
            )
            fetch_id = store.record_fetch(source="catalog", method="GET", url="test")
            store.store_response(
                table_name="catalog_teach_lesson_list_for_teach",
                response=response,
                fetch_id=fetch_id,
            )

            mapping = _stored_course_ids_by_code(store)
            stored_ids = store.conn.execute(
                """
                SELECT lesson.id, course.id
                FROM catalog_teach_lesson_list_for_teach AS lesson
                JOIN catalog_teach_lesson_list_for_teach_course AS course
                  ON course.parent_store_id = lesson.store_id
                """
            ).fetchone()
        finally:
            store.close()

        self.assertEqual(mapping, {"MATH1001": 144481})
        self.assertEqual(stored_ids, (181384, 144481))

    def test_rejects_conflicting_course_id_and_code_mappings(self) -> None:
        conflicting_code = TeachLessonListResponse(
            root=[
                _catalog_lesson(1, course_id=10, course_code="A"),
                _catalog_lesson(2, course_id=11, course_code="A"),
            ]
        )
        conflicting_id = TeachLessonListResponse(
            root=[
                _catalog_lesson(1, course_id=10, course_code="A"),
                _catalog_lesson(2, course_id=10, course_code="B"),
            ]
        )

        with self.assertRaisesRegex(ValueError, "maps to both"):
            _course_ids_by_code_from_response(conflicting_code)
        with self.assertRaisesRegex(ValueError, "maps to both"):
            _course_ids_by_code_from_response(conflicting_id)

    def test_rejects_changed_course_id_from_previous_snapshot(self) -> None:
        response = TeachLessonListResponse(
            root=[_catalog_lesson(1, course_id=11, course_code="A")]
        )

        with self.assertRaisesRegex(ValueError, "changed id from 10 to 11"):
            _course_ids_by_code_from_response(
                response, previous_course_ids_by_code={"A": 10}
            )

    def test_rejects_duplicate_lesson_and_missing_course(self) -> None:
        duplicate_lesson = TeachLessonListResponse(
            root=[
                _catalog_lesson(1, course_id=10, course_code="A"),
                _catalog_lesson(1, course_id=10, course_code="A"),
            ]
        )
        missing_course = TeachLessonListResponse(
            root=[_catalog_lesson(1, course_id=None, course_code=None)]
        )

        with self.assertRaisesRegex(ValueError, "Duplicate catalog lesson"):
            _course_ids_by_code_from_response(duplicate_lesson)
        with self.assertRaisesRegex(ValueError, "has no valid course"):
            _course_ids_by_code_from_response(missing_course)

    def test_rejects_course_without_chinese_name(self) -> None:
        response = TeachLessonListResponse(
            root=[
                _catalog_lesson(
                    1,
                    course_id=10,
                    course_code="A",
                    course_cn=None,
                )
            ]
        )

        with self.assertRaisesRegex(ValueError, "has no valid course name"):
            _course_ids_by_code_from_response(response)

    def test_rejects_multiple_stored_courses_for_one_lesson(self) -> None:
        response = TeachLessonListResponse(
            root=[_catalog_lesson(1, course_id=10, course_code="A")]
        )
        store = SQLiteModelStore(":memory:")
        try:
            store.register_response_model(
                table_name="catalog_teach_lesson_list_for_teach",
                response_model=TeachLessonListResponse,
            )
            fetch_id = store.record_fetch(source="catalog", method="GET", url="test")
            store.store_response(
                table_name="catalog_teach_lesson_list_for_teach",
                response=response,
                fetch_id=fetch_id,
            )
            parent_store_id = store.conn.execute(
                "SELECT store_id FROM catalog_teach_lesson_list_for_teach"
            ).fetchone()[0]
            store.conn.execute(
                """
                INSERT INTO catalog_teach_lesson_list_for_teach_course(
                    fetch_id, parent_store_id, id, code, cn, en
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (fetch_id, parent_store_id, 10, "A", None, None),
            )

            with self.assertRaisesRegex(ValueError, "has 2 courses"):
                _stored_course_ids_by_code(store)
        finally:
            store.close()


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
    def test_skips_structured_bad_gateway_errors(self) -> None:
        request = httpx.Request(
            "GET", "https://catalog.ustc.edu.cn/api/teach/exam/list/401"
        )
        for status_code in (502, 504):
            response = httpx.Response(status_code, request=request)
            error = httpx.HTTPStatusError(
                "Bad Gateway",
                request=request,
                response=response,
            )
            self.assertTrue(_is_skippable_exam_fetch_error(error))

    def test_does_not_skip_other_structured_http_errors(self) -> None:
        request = httpx.Request(
            "GET", "https://catalog.ustc.edu.cn/api/teach/exam/list/401"
        )
        response = httpx.Response(500, request=request)
        error = httpx.HTTPStatusError(
            "Server Error",
            request=request,
            response=response,
        )

        self.assertFalse(_is_skippable_exam_fetch_error(error))

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
