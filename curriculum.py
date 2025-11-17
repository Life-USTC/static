import asyncio
import os
from tqdm import tqdm
from patchright.async_api import Page

from models.course import Course
from utils.catalog import get_semesters, get_courses, get_exams
from utils.jw import update_lectures
from utils.tools import save_json
from utils.auth import USTCAuth


async def fetch_course_info(
    page: Page,
    semester_path: str,
    _courses: list[Course],
    sem,
    progress_bar,
    course_api_path: str,
):
    async with sem:
        _courses = await update_lectures(page, _courses)

        for _course in _courses:
            save_json(_course, os.path.join(semester_path, f"{_course.id}.json"))
            save_json(_course, os.path.join(course_api_path, f"{_course.id}"))

        progress_bar.update(len(_courses))


async def fetch_semester(
    page: Page,
    curriculum_path: str,
    semester_id: str,
    course_api_path: str,
):
    # create "$(base_dir)/build/curriculum/$(semester_id)" directory if not exists
    semester_path = os.path.join(curriculum_path, semester_id)
    if not os.path.exists(semester_path):
        os.mkdir(semester_path)

    try:
        courses = await get_courses(page=page, semester_id=semester_id)
    except Exception as e:
        print(f"Failed to get courses for semester {semester_id}: {e}")
        return

    save_json(courses, os.path.join(semester_path, "courses.json"))

    if int(semester_id) >= 221:
        try:
            exams = await get_exams(page=page, semester_id=semester_id)
        except Exception as e:
            print(f"Failed to get exams for semester {semester_id}: {e}")
            exams = {}

        for course in courses:
            if course.id in exams.keys():
                course.exams = exams[course.id]

    sem = asyncio.Semaphore(50)
    progress_bar = tqdm(
        total=len(courses),
        position=0,
        leave=True,
        desc=f"Processing semester id={semester_id}",
    )
    course_chunks = [courses[i : i + 50] for i in range(0, len(courses), 50)]
    tasks = [
        fetch_course_info(
            page,
            semester_path,
            _courses,
            sem,
            progress_bar,
            course_api_path,
        )
        for _courses in course_chunks
    ]

    with progress_bar:
        await asyncio.gather(*tasks)


async def make_curriculum():
    base_path = os.path.dirname(os.path.abspath(__file__))
    curriculum_path = os.path.join(base_path, "build", "curriculum")
    course_api_path = os.path.join(base_path, "build", "api", "course")
    if not os.path.exists(curriculum_path):
        os.makedirs(curriculum_path)

    if not os.path.exists(course_api_path):
        os.makedirs(course_api_path)

    async with USTCAuth() as page:
        semesters = await get_semesters(page=page)
        semesters = [
            semester for semester in semesters if int(semester.id) >= 401
        ]  # dropping semester before 2025

        save_json(semesters, os.path.join(curriculum_path, "semesters.json"))

        for semester in tqdm(
            semesters,
            position=1,
            leave=True,
            desc="Processing semesters",
        ):
            await fetch_semester(
                page, curriculum_path, str(semester.id), course_api_path
            )


def main():
    asyncio.run(make_curriculum())


if __name__ == "__main__":
    main()
