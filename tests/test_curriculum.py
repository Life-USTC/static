import unittest

from src.curriculum import _should_fetch_catalog_exams


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
