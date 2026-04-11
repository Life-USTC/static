import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from tqdm import tqdm

from .models.course import Course
from .models.semester import Semester
from .utils.auth import RequestSession, USTCSession
from .utils.catalog import get_exams, get_semesters
from .utils.jw import get_courses, get_jw_semesters, update_lectures
from .utils.tools import BUILD_DIR, raw_date_to_unix_timestamp, save_json, tz

logger = logging.getLogger(__name__)

SemesterPullMode = Literal["all", "window"]


def _has_existing_curriculum_data(curriculum_path: Path) -> bool:
    if not curriculum_path.exists():
        return False

    for semester_path in curriculum_path.iterdir():
        if (
            semester_path.is_dir()
            and semester_path.name.isdigit()
            and (semester_path / "courses.json").exists()
        ):
            return True

    return False


def _load_existing_semesters(curriculum_path: Path) -> list[Semester]:
    semesters_path = curriculum_path / "semesters.json"
    if not semesters_path.exists():
        return []

    try:
        payload = json.loads(semesters_path.read_text())
        return [Semester.model_validate(item) for item in payload]
    except Exception as e:
        logger.warning("Failed to load cached semester metadata: %s", e)
        return []


def _semester_sort_key(semester: Semester) -> tuple[int, str]:
    if semester.id.isdigit():
        return int(semester.id), semester.id
    return -1, semester.id


def _merge_semesters(*semester_groups: list[Semester]) -> list[Semester]:
    merged: dict[str, Semester] = {}

    for semesters in semester_groups:
        for semester in semesters:
            if not semester.id:
                continue

            current = merged.get(semester.id)
            if current is None:
                merged[semester.id] = semester
                continue

            merged[semester.id] = Semester(
                id=semester.id,
                courses=semester.courses or current.courses,
                name=semester.name or current.name,
                startDate=semester.startDate or current.startDate,
                endDate=semester.endDate or current.endDate,
            )

    return sorted(merged.values(), key=_semester_sort_key)


def _shift_year(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year + years)


def _filter_semesters_for_window(
    semesters: list[Semester], *, window_years: int
) -> list[Semester]:
    today = datetime.now(tz).date()
    window_start = _shift_year(today, -window_years)
    window_end = _shift_year(today, window_years)
    window_start_ts = raw_date_to_unix_timestamp(window_start.isoformat())
    window_end_ts = raw_date_to_unix_timestamp(window_end.isoformat())

    return [
        semester
        for semester in semesters
        if window_start_ts <= semester.startDate <= window_end_ts
    ]


def _select_semesters(
    semesters: list[Semester],
    *,
    mode: SemesterPullMode,
    window_years: int,
    curriculum_path: Path,
) -> list[Semester]:
    if mode == "all":
        return semesters

    if _has_existing_curriculum_data(curriculum_path):
        return _filter_semesters_for_window(semesters, window_years=window_years)

    logger.info(
        "No cached curriculum data found; falling back to a full semester refresh"
    )
    return semesters


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
        logger.exception("Failed to get courses for semester %s: %s", semester_id, e)
        return

    save_json(incomplete_courses, semester_path / "courses.json")

    try:
        exams = await get_exams(session=session, semester_id=semester_id)
    except Exception as e:
        logger.exception("Failed to get exams for semester %s: %s", semester_id, e)
        exams = {}

    for course in incomplete_courses:
        if course.id in exams:
            course.exams = exams[course.id]

    sem = asyncio.Semaphore(10)
    progress_bar = tqdm(
        total=len(incomplete_courses),
        position=0,
        leave=True,
        desc=f"Processing semester id={semester_id}",
    )
    incomplete_courses_chunks = [
        incomplete_courses[i : i + 100] for i in range(0, len(incomplete_courses), 100)
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


async def make_curriculum(
    *, mode: SemesterPullMode = "all", window_years: int = 1
) -> None:
    curriculum_path = BUILD_DIR / "curriculum"
    course_api_path = BUILD_DIR / "api" / "course"
    if not curriculum_path.exists():
        curriculum_path.mkdir(parents=True)

    if not course_api_path.exists():
        course_api_path.mkdir(parents=True)

    async with USTCSession() as session:
        existing_semesters = _load_existing_semesters(curriculum_path)
        catalog_semesters = await get_semesters(session=session)
        jw_semesters: list[Semester] = []

        if mode == "all" or not _has_existing_curriculum_data(curriculum_path):
            jw_semesters = await get_jw_semesters(session=session)

        semesters = _merge_semesters(
            existing_semesters,
            jw_semesters,
            catalog_semesters,
        )
        save_json(semesters, curriculum_path / "semesters.json")

        semesters = _select_semesters(
            semesters,
            mode=mode,
            window_years=window_years,
            curriculum_path=curriculum_path,
        )

        logger.info(
            (
                "Discovered %s semester(s): catalog=%s jw=%s cached=%s; "
                "refreshing %s semester(s) with mode=%s window_years=%s"
            ),
            len(_merge_semesters(catalog_semesters, jw_semesters, existing_semesters)),
            len(catalog_semesters),
            len(jw_semesters),
            len(existing_semesters),
            len(semesters),
            mode,
            window_years,
        )

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
