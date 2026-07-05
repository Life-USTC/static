import unittest

from src.curriculum import (
    _selected_curriculum_semesters,
    _should_fetch_catalog_exams,
    _should_fetch_catalog_lessons,
)
from src.models.semester import Semester


def _semester(semester_id: str) -> Semester:
    return Semester(
        id=semester_id,
        courses=[],
        name=f"semester {semester_id}",
        startDate=0,
        endDate=0,
    )


class CatalogLessonFetchTest(unittest.TestCase):
    def test_skips_semesters_below_minimum_lesson_id(self) -> None:
        self.assertFalse(_should_fetch_catalog_lessons("53"))
        self.assertFalse(_should_fetch_catalog_lessons("61"))

    def test_fetches_semesters_at_or_above_minimum_lesson_id(self) -> None:
        self.assertTrue(_should_fetch_catalog_lessons("62"))
        self.assertTrue(_should_fetch_catalog_lessons("381"))

    def test_fetches_non_numeric_semester_ids(self) -> None:
        self.assertTrue(_should_fetch_catalog_lessons("latest"))

    def test_selected_curriculum_semesters_filter_legacy_ids(self) -> None:
        selected = _selected_curriculum_semesters(
            [_semester("53"), _semester("61"), _semester("62"), _semester("381")]
        )

        self.assertEqual([semester.id for semester in selected], ["62", "381"])


class CatalogExamFetchTest(unittest.TestCase):
    def test_skips_semesters_below_minimum_exam_id(self) -> None:
        self.assertFalse(_should_fetch_catalog_exams("221"))
        self.assertFalse(_should_fetch_catalog_exams("362"))

    def test_fetches_semesters_at_or_above_minimum_exam_id(self) -> None:
        self.assertTrue(_should_fetch_catalog_exams("381"))
        self.assertTrue(_should_fetch_catalog_exams("401"))

    def test_fetches_non_numeric_semester_ids(self) -> None:
        self.assertTrue(_should_fetch_catalog_exams("latest"))


if __name__ == "__main__":
    unittest.main()
