import logging
from json import JSONDecodeError

from patchright.async_api import Error
from tqdm import tqdm

from .guesses import SQLiteGuessStore
from .models.api.catalog_api_teach_department_college_tree import (
    DepartmentCollegeTreeResponse,
)
from .models.api.catalog_api_teach_exam_list import TeachExamListResponse
from .models.api.catalog_api_teach_lesson_list_for_teach import (
    TeachLessonListResponse,
)
from .models.api.catalog_api_teach_semester_list import TeachSemesterListResponse
from .models.api.jw_ws_schedule_table_datum import JwWsScheduleTableDatumResponse
from .models.course import Course
from .models.semester import Semester
from .sqlite_store import GUESSES_FILENAME, SNAPSHOT_FILENAME, SQLiteModelStore
from .upstream_contracts import UPSTREAM_RESPONSE_MODELS
from .utils.auth import RequestSession, USTCSession
from .utils.catalog import (
    fetch_courses_json,
    fetch_departments_json,
    fetch_exams_json,
    fetch_semesters_json,
    parse_courses,
    parse_semesters,
)
from .utils.jw import fetch_jw_schedule_table_json
from .utils.tools import BUILD_DIR

logger = logging.getLogger(__name__)

CATALOG_SEMESTER_URL = "https://catalog.ustc.edu.cn/api/teach/semester/list"
CATALOG_DEPARTMENT_URL = "https://catalog.ustc.edu.cn/api/teach/department/college-tree"
CATALOG_LESSON_URL_PREFIX = "https://catalog.ustc.edu.cn/api/teach/lesson/list-for-teach"
CATALOG_EXAM_URL_PREFIX = "https://catalog.ustc.edu.cn/api/teach/exam/list"
JW_SCHEDULE_TABLE_URL = "https://jw.ustc.edu.cn/ws/schedule-table/datum"
MIN_JW_SCHEDULE_SEMESTER_ID = 100


def _course_chunks(courses: list[Course], chunk_size: int = 100) -> list[list[Course]]:
    return [courses[i : i + chunk_size] for i in range(0, len(courses), chunk_size)]


def _should_fetch_jw_schedule_table(semester_id: str) -> bool:
    try:
        return int(semester_id) >= MIN_JW_SCHEDULE_SEMESTER_ID
    except ValueError:
        return True


def _is_skippable_exam_fetch_error(error: Error) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            " 502 ",
            " 504 ",
            "502 proxy error",
            "504 gateway time-out",
            "gateway time-out",
        )
    )


def _register_upstream_tables(store: SQLiteModelStore) -> None:
    for table_name, response_model in UPSTREAM_RESPONSE_MODELS.items():
        store.register_response_model(
            table_name=table_name,
            response_model=response_model,
        )


async def _store_catalog_semesters(
    session: RequestSession, store: SQLiteModelStore
) -> list[Semester]:
    payload = await fetch_semesters_json(session=session)
    response = TeachSemesterListResponse.model_validate(payload)
    fetch_id = store.record_fetch(
        source="catalog_teach_semester_list",
        method="GET",
        url=CATALOG_SEMESTER_URL,
    )
    count = store.store_response(
        table_name="catalog_teach_semester_list",
        response=response,
        fetch_id=fetch_id,
    )
    store.put_metadata({"catalog_teach_semester_list_count": count})
    return parse_semesters(payload)


async def _store_catalog_departments(
    session: RequestSession, store: SQLiteModelStore
) -> None:
    payload = await fetch_departments_json(session=session)
    response = DepartmentCollegeTreeResponse.model_validate(payload)
    fetch_id = store.record_fetch(
        source="catalog_teach_department_college_tree",
        method="GET",
        url=CATALOG_DEPARTMENT_URL,
    )
    count = store.store_response(
        table_name="catalog_teach_department_college_tree",
        response=response,
        fetch_id=fetch_id,
    )
    store.put_metadata({"catalog_teach_department_college_tree_count": count})


async def _store_catalog_exams(
    *,
    session: RequestSession,
    store: SQLiteModelStore,
    semester_id: str,
) -> None:
    url = f"{CATALOG_EXAM_URL_PREFIX}/{semester_id}"
    try:
        payload = await fetch_exams_json(session=session, semester_id=semester_id)
    except Error as e:
        if not _is_skippable_exam_fetch_error(e):
            raise
        store.record_fetch(
            source="catalog_teach_exam_list",
            method="GET",
            url=url,
            context={"semester_id": semester_id},
            ok=False,
            error=str(e),
        )
        logger.warning(
            "Skipping catalog exams for semester %s after upstream 502/504",
            semester_id,
        )
        return

    response = TeachExamListResponse.model_validate(payload)
    fetch_id = store.record_fetch(
        source="catalog_teach_exam_list",
        method="GET",
        url=url,
        context={"semester_id": semester_id},
    )
    store.store_response(
        table_name="catalog_teach_exam_list",
        response=response,
        fetch_id=fetch_id,
        context={"semester_id": semester_id},
    )


async def _store_jw_schedule_chunks(
    *,
    session: RequestSession,
    store: SQLiteModelStore,
    guesses: SQLiteGuessStore,
    semester_id: str,
    catalog_response: TeachLessonListResponse,
    courses: list[Course],
) -> None:
    if not _should_fetch_jw_schedule_table(semester_id):
        logger.info(
            "Skipping JW schedule table for legacy semester %s below minimum id %s",
            semester_id,
            MIN_JW_SCHEDULE_SEMESTER_ID,
        )
        guesses.add_teacher_section_guesses(
            semester_id=semester_id,
            catalog_lessons=catalog_response,
            jw_schedules=None,
        )
        return

    chunks = _course_chunks(courses)
    if not chunks:
        guesses.add_teacher_section_guesses(
            semester_id=semester_id,
            catalog_lessons=catalog_response,
            jw_schedules=None,
        )
        return

    schedule_responses: list[JwWsScheduleTableDatumResponse] = []
    for chunk_index, chunk in enumerate(chunks):
        try:
            payload = await fetch_jw_schedule_table_json(
                session=session,
                course_list=chunk,
            )
        except JSONDecodeError as e:
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url=JW_SCHEDULE_TABLE_URL,
                context={"semester_id": semester_id, "chunk_index": chunk_index},
                ok=False,
                error=str(e),
            )
            logger.info(
                "Skipping remaining JW schedule table chunks for semester %s after "
                "non-JSON response at chunk %s",
                semester_id,
                chunk_index,
            )
            break

        response = JwWsScheduleTableDatumResponse.model_validate(payload)
        schedule_responses.append(response)
        fetch_id = store.record_fetch(
            source="jw_ws_schedule_table_datum",
            method="POST",
            url=JW_SCHEDULE_TABLE_URL,
            context={"semester_id": semester_id, "chunk_index": chunk_index},
        )
        store.store_response(
            table_name="jw_ws_schedule_table_datum",
            response=response,
            fetch_id=fetch_id,
            context={"semester_id": semester_id, "chunk_index": chunk_index},
        )

    guesses.add_teacher_section_guesses(
        semester_id=semester_id,
        catalog_lessons=catalog_response,
        jw_schedules=schedule_responses,
    )


async def _store_semester(
    *,
    session: RequestSession,
    store: SQLiteModelStore,
    guesses: SQLiteGuessStore,
    semester_id: str,
) -> None:
    payload = await fetch_courses_json(session=session, semester_id=semester_id)
    catalog_response = TeachLessonListResponse.model_validate(payload)
    fetch_id = store.record_fetch(
        source="catalog_teach_lesson_list_for_teach",
        method="GET",
        url=f"{CATALOG_LESSON_URL_PREFIX}/{semester_id}",
        context={"semester_id": semester_id},
    )
    lesson_count = store.store_response(
        table_name="catalog_teach_lesson_list_for_teach",
        response=catalog_response,
        fetch_id=fetch_id,
        context={"semester_id": semester_id},
    )
    logger.info("Stored %s catalog lessons for semester %s", lesson_count, semester_id)

    courses = parse_courses(payload)
    await _store_catalog_exams(session=session, store=store, semester_id=semester_id)
    await _store_jw_schedule_chunks(
        session=session,
        store=store,
        guesses=guesses,
        semester_id=semester_id,
        catalog_response=catalog_response,
        courses=courses,
    )


async def make_curriculum() -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    store = SQLiteModelStore(BUILD_DIR / SNAPSHOT_FILENAME)
    guesses = SQLiteGuessStore(BUILD_DIR / GUESSES_FILENAME)

    try:
        _register_upstream_tables(store)
        async with USTCSession() as session:
            semesters = await _store_catalog_semesters(session=session, store=store)
            await _store_catalog_departments(session=session, store=store)
            store.put_metadata(
                {
                    "curriculum_mode": "all",
                    "selected_semester_count": len(semesters),
                    "jw_schedule_min_semester_id": MIN_JW_SCHEDULE_SEMESTER_ID,
                    "jw_schedule_selected_semester_count": sum(
                        _should_fetch_jw_schedule_table(str(semester.id))
                        for semester in semesters
                    ),
                    "jw_schedule_skipped_legacy_semester_count": sum(
                        not _should_fetch_jw_schedule_table(str(semester.id))
                        for semester in semesters
                    ),
                }
            )

            logger.info(
                "Discovered %s semester(s); refreshing complete SQLite snapshot",
                len(semesters),
            )

            for semester in tqdm(
                semesters,
                position=1,
                leave=True,
                desc="Processing semesters",
            ):
                await _store_semester(
                    session=session,
                    store=store,
                    guesses=guesses,
                    semester_id=str(semester.id),
                )
    finally:
        store.close()
        guesses.close()
