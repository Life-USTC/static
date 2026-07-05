import unittest

from src.curriculum import (
    _cached_complete_semester_ids,
    _refresh_curriculum_semesters,
    _selected_curriculum_semesters,
    _semester_has_ended,
    _should_fetch_catalog_exams,
    _should_fetch_catalog_lessons,
    _should_fetch_jw_schedule_table,
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
