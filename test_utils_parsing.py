import json
import unittest
from pathlib import Path

from src.models import Course
from src.utils.catalog import parse_courses, parse_exams, parse_semesters
from src.utils.jw import parse_jw_courses, parse_jw_schedule_table

ROOT_DIR = Path(__file__).resolve().parent
CACHE_DIR = ROOT_DIR / "build" / "cache"


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class TestCatalogParsing(unittest.TestCase):
    def test_parse_semesters(self):
        semesters_dir = CACHE_DIR / "catalog" / "api" / "teach" / "semester"
        for json_file in semesters_dir.glob("*.json"):
            payload = load_json(json_file)
            parse_semesters(payload)

    def test_parse_courses(self):
        courses_dir = (
            CACHE_DIR / "catalog" / "api" / "teach" / "lesson" / "list-for-teach"
        )
        for json_file in courses_dir.glob("*.json"):
            payload = load_json(json_file)
            parse_courses(payload)

    def test_parse_exams(self):
        exams_dir = CACHE_DIR / "catalog" / "api" / "teach" / "exam" / "list"
        for json_file in exams_dir.glob("*.json"):
            payload = load_json(json_file)
            parse_exams(payload)


class TestJwParsing(unittest.TestCase):
    def test_parse_jw_courses(self):
        jw_courses_dir = CACHE_DIR / "jw" / "for-std" / "lesson-search" / "semester"
        for semester_dir in jw_courses_dir.glob("*"):
            search_dir = semester_dir / "search"
            if not search_dir.exists():
                continue
            for json_file in search_dir.glob("*.json"):
                payload = load_json(json_file)
                parse_jw_courses(payload)

    def test_parse_jw_schedule_table(self):
        # Loop through all JSON files in the directory
        schedule_dir = CACHE_DIR / "jw" / "ws" / "schedule-table" / "datum"
        for json_file in schedule_dir.glob("*.json"):
            payload = load_json(json_file)
            lesson_item = payload["result"]["lessonList"][0]
            course = Course(
                id=lesson_item["id"],
                name=lesson_item["courseName"],
                courseCode=str(lesson_item["courseId"]),
                lessonCode=lesson_item["code"],
                teacherName="",
                lectures=[],
                exams=[],
                dateTimePlacePersonText=None,
                courseType=None,
                courseGradation="",
                courseCategory="",
                educationType="",
                classType="",
                openDepartment="",
                description="",
                credit=0.0,
                additionalInfo={},
            )

            parse_jw_schedule_table([course], payload, cache_url=None)


if __name__ == "__main__":
    unittest.main()
