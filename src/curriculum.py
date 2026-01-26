import asyncio
from pathlib import Path

from tqdm import tqdm

from .models.course import Course
from .utils.auth import RequestSession, USTCSession
from .utils.catalog import get_courses, get_exams, get_semesters
from .utils.jw import update_lectures
from .utils.tools import BUILD_DIR, save_json


async def fetch_semester(
    session: RequestSession,
    curriculum_path: Path,
    semester_id: str,
    course_api_path: Path,
):
    semester_path = curriculum_path / semester_id
    if not semester_path.exists():
        semester_path.mkdir()

    try:
        incomplete_courses = await get_courses(session=session, semester_id=semester_id)
    except Exception as e:
        print(f"Failed to get courses for semester {semester_id}: {e}")
        return

    save_json(incomplete_courses, semester_path / "courses.json")

    if int(semester_id) >= 221:
        try:
            exams = await get_exams(session=session, semester_id=semester_id)
        except Exception as e:
            print(f"Failed to get exams for semester {semester_id}: {e}")
            exams = {}

        for course in incomplete_courses:
            if course.id in exams:
                course.exams = exams[course.id]

    sem = asyncio.Semaphore(50)
    progress_bar = tqdm(
        total=len(incomplete_courses),
        position=0,
        leave=True,
        desc=f"Processing semester id={semester_id}",
    )
    incomplete_courses_chunks = [
        incomplete_courses[i : i + 50] for i in range(0, len(incomplete_courses), 50)
    ]

    async def fetch_course_info(
        session: RequestSession,
        semester_path: Path,
        incomplete_courses: list[Course],
        sem,
        progress_bar,
        course_api_path: Path,
    ):
        async with sem:
            courses = await update_lectures(session, incomplete_courses)

            for course in courses:
                save_json(course, semester_path / f"{course.id}.json")
                save_json(course, course_api_path / f"{course.id}")

            progress_bar.update(len(incomplete_courses))

    tasks = [
        fetch_course_info(
            session,
            semester_path,
            incomplete_courses_chunk,
            sem,
            progress_bar,
            course_api_path,
        )
        for incomplete_courses_chunk in incomplete_courses_chunks
    ]

    with progress_bar:
        await asyncio.gather(*tasks)


async def make_curriculum() -> None:
    curriculum_path = BUILD_DIR / "curriculum"
    course_api_path = BUILD_DIR / "api" / "course"
    if not curriculum_path.exists():
        curriculum_path.mkdir(parents=True)

    if not course_api_path.exists():
        course_api_path.mkdir(parents=True)

    async with USTCSession() as session:
        semesters = await get_semesters(session=session)
        save_json(semesters, curriculum_path / "semesters.json")

        semesters = [semester for semester in semesters if int(semester.id) >= 401]

        for semester in tqdm(
            semesters,
            position=1,
            leave=True,
            desc="Processing semesters",
        ):
            await fetch_semester(
                session,
                curriculum_path,
                str(semester.id),
                course_api_path,
            )
