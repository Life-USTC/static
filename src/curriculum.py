import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from json import JSONDecodeError

import httpx
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
from .observed_contracts import (
    ObservedContractCollector,
    log_contract_diagnostics,
    publish_contract_artifacts,
)
from .sqlite_store import GUESSES_FILENAME, SNAPSHOT_FILENAME, SQLiteModelStore
from .upstream_contracts import (
    CATALOG_DEPARTMENTS,
    CATALOG_EXAMS,
    CATALOG_LESSONS,
    CATALOG_SEMESTERS,
    CURRICULUM_UPSTREAM_RESPONSE_MODELS,
    JW_SCHEDULES,
    UPSTREAM_RESPONSE_MODELS,
)
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
from .utils.tools import BASE_DIR, BUILD_DIR

logger = logging.getLogger(__name__)

CATALOG_SEMESTER_URL = "https://catalog.ustc.edu.cn/api/teach/semester/list"
CATALOG_DEPARTMENT_URL = "https://catalog.ustc.edu.cn/api/teach/department/college-tree"
CATALOG_LESSON_URL_PREFIX = (
    "https://catalog.ustc.edu.cn/api/teach/lesson/list-for-teach"
)
CATALOG_EXAM_URL_PREFIX = "https://catalog.ustc.edu.cn/api/teach/exam/list"
JW_SCHEDULE_TABLE_URL = "https://jw.ustc.edu.cn/ws/schedule-table/datum"
ENDED_SEMESTER_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
CATALOG_EXAM_TIMEOUT_MS = 60_000
CURRICULUM_FETCH_CONCURRENCY = 3
JW_SCHEDULE_CHUNK_SIZE = 100
JW_SCHEDULE_EXPECTED_CHUNK_COUNT_KEY_PREFIX = "jw_schedule_expected_chunk_count_"
CATALOG_LESSON_TABLE = "catalog_teach_lesson_list_for_teach"
CATALOG_COURSE_TABLE = f"{CATALOG_LESSON_TABLE}_course"


def _course_ids_by_code_from_response(
    response: TeachLessonListResponse,
    *,
    previous_course_ids_by_code: dict[str, int] | None = None,
) -> dict[str, int]:
    lessons = response.root
    if lessons is None:
        raise ValueError("Catalog lesson response must be a list")

    lesson_ids: set[int] = set()
    course_ids_by_code: dict[str, int] = {}
    course_codes_by_id: dict[int, str] = {}
    for position, lesson in enumerate(lessons):
        if lesson.id is None or lesson.id <= 0:
            raise ValueError(f"Catalog lesson at position {position} has no valid id")
        if lesson.id in lesson_ids:
            raise ValueError(f"Duplicate catalog lesson id {lesson.id}")
        lesson_ids.add(lesson.id)

        course = lesson.course
        if course is None or course.id is None or course.id <= 0:
            raise ValueError(f"Catalog lesson {lesson.id} has no valid course")
        code = (course.code or "").strip()
        if not code:
            raise ValueError(f"Catalog lesson {lesson.id} has no valid course code")
        if not (course.cn or "").strip():
            raise ValueError(f"Catalog lesson {lesson.id} has no valid course name")

        existing_id = course_ids_by_code.get(code)
        if existing_id is not None and existing_id != course.id:
            raise ValueError(
                f"Catalog course code {code} maps to both {existing_id} and {course.id}"
            )
        existing_code = course_codes_by_id.get(course.id)
        if existing_code is not None and existing_code != code:
            raise ValueError(
                f"Catalog course id {course.id} maps to both {existing_code} and {code}"
            )
        course_ids_by_code[code] = course.id
        course_codes_by_id[course.id] = code

    for code, course_id in course_ids_by_code.items():
        previous_id = (previous_course_ids_by_code or {}).get(code)
        if previous_id is not None and previous_id != course_id:
            raise ValueError(
                f"Catalog course code {code} changed id from "
                f"{previous_id} to {course_id}"
            )

    return course_ids_by_code


def _stored_course_ids_by_code(store: SQLiteModelStore) -> dict[str, int]:
    invalid_lesson = store.conn.execute(
        f"""
        SELECT lesson.store_id, lesson.id, COUNT(course.store_id)
        FROM {CATALOG_LESSON_TABLE} AS lesson
        LEFT JOIN {CATALOG_COURSE_TABLE} AS course
          ON course.parent_store_id = lesson.store_id
        GROUP BY lesson.store_id, lesson.id
        HAVING lesson.id IS NULL OR lesson.id <= 0 OR COUNT(course.store_id) != 1
        LIMIT 1
        """
    ).fetchone()
    if invalid_lesson is not None:
        store_id, lesson_id, course_count = invalid_lesson
        raise ValueError(
            "Stored catalog lesson "
            f"{lesson_id!r} (store_id={store_id}) has {course_count} courses"
        )

    duplicate_lesson = store.conn.execute(
        f"""
        SELECT id, COUNT(*)
        FROM {CATALOG_LESSON_TABLE}
        GROUP BY id
        HAVING COUNT(*) != 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate_lesson is not None:
        lesson_id, count = duplicate_lesson
        raise ValueError(f"Stored catalog lesson id {lesson_id} occurs {count} times")

    rows = store.conn.execute(
        f"""
        SELECT course.id, course.code, course.cn
        FROM {CATALOG_LESSON_TABLE} AS lesson
        JOIN {CATALOG_COURSE_TABLE} AS course
          ON course.parent_store_id = lesson.store_id
        """
    ).fetchall()
    course_ids_by_code: dict[str, int] = {}
    course_codes_by_id: dict[int, str] = {}
    for course_id, raw_code, raw_cn in rows:
        code = (raw_code or "").strip()
        valid_name = bool((raw_cn or "").strip())
        if course_id is None or course_id <= 0 or not code or not valid_name:
            raise ValueError(
                f"Stored catalog course has invalid id/code/name: {course_id!r}"
            )
        existing_id = course_ids_by_code.get(code)
        if existing_id is not None and existing_id != course_id:
            raise ValueError(
                f"Stored catalog course code {code} maps to both "
                f"{existing_id} and {course_id}"
            )
        existing_code = course_codes_by_id.get(course_id)
        if existing_code is not None and existing_code != code:
            raise ValueError(
                f"Stored catalog course id {course_id} maps to both "
                f"{existing_code} and {code}"
            )
        course_ids_by_code[code] = course_id
        course_codes_by_id[course_id] = code

    return course_ids_by_code


def _course_chunks(
    courses: list[Course], chunk_size: int = JW_SCHEDULE_CHUNK_SIZE
) -> list[list[Course]]:
    return [courses[i : i + chunk_size] for i in range(0, len(courses), chunk_size)]


def _semester_sort_key(semester: Semester) -> int:
    try:
        return int(semester.id)
    except ValueError:
        return 0


def _semester_has_ended(semester: Semester, now_timestamp: float) -> bool:
    return semester.endDate > 0 and semester.endDate < now_timestamp


def _refresh_curriculum_semesters(
    semesters: list[Semester],
    *,
    cached_semester_ids: set[str],
    now_timestamp: float,
) -> list[Semester]:
    return [
        semester
        for semester in semesters
        if not _semester_has_ended(semester, now_timestamp)
        or str(semester.id) not in cached_semester_ids
    ]


def _curriculum_semesters_to_refresh(
    semesters: list[Semester],
    *,
    cached_semester_ids: set[str],
    now_timestamp: float,
    verify_upstream_contract: bool,
) -> list[Semester]:
    if verify_upstream_contract:
        return semesters
    return _refresh_curriculum_semesters(
        semesters,
        cached_semester_ids=cached_semester_ids,
        now_timestamp=now_timestamp,
    )


def _cached_fresh_lesson_semester_ids(
    store: SQLiteModelStore, semesters: list[Semester], *, now_timestamp: float
) -> set[str]:
    return {
        str(semester.id)
        for semester in semesters
        if _has_cached_catalog_lessons(store, str(semester.id))
        and _has_cached_jw_schedule(store, str(semester.id))
        and _semester_cache_is_fresh(
            store,
            str(semester.id),
            now_timestamp,
            sources={
                "catalog_teach_lesson_list_for_teach",
                "jw_ws_schedule_table_datum",
            },
        )
    }


def _cached_fresh_exam_semester_ids(
    store: SQLiteModelStore, semesters: list[Semester], *, now_timestamp: float
) -> set[str]:
    return {
        str(semester.id)
        for semester in semesters
        if _has_cached_catalog_exams(store, str(semester.id))
        and _semester_cache_is_fresh(
            store, str(semester.id), now_timestamp, sources={"catalog_teach_exam_list"}
        )
    }


def _semester_cache_is_fresh(
    store: SQLiteModelStore,
    semester_id: str,
    now_timestamp: float,
    *,
    sources: set[str],
) -> bool:
    fetches = store.conn.execute(
        """
        SELECT source, context, fetched_at FROM upstream_fetches
        WHERE source IN ('catalog_teach_lesson_list_for_teach',
                         'catalog_teach_exam_list', 'jw_ws_schedule_table_datum')
        """
    )
    timestamps = []
    for source, context, fetched_at in fetches:
        if (
            source not in sources
            or _fetch_context_values(context).get("semester_id") != semester_id
        ):
            continue
        try:
            parsed = datetime.fromisoformat(fetched_at)
            if parsed.tzinfo is None:
                return False
            timestamps.append(parsed.timestamp())
        except (TypeError, ValueError):
            return False
    return bool(timestamps) and all(
        0 <= now_timestamp - timestamp < ENDED_SEMESTER_CACHE_MAX_AGE_SECONDS
        for timestamp in timestamps
    )


def _has_cached_catalog_lessons(store: SQLiteModelStore, semester_id: str) -> bool:
    return _has_cached_source_semester(
        store,
        source="catalog_teach_lesson_list_for_teach",
        semester_id=semester_id,
    )


def _has_cached_catalog_exams(store: SQLiteModelStore, semester_id: str) -> bool:
    return _has_cached_source_semester(
        store,
        source="catalog_teach_exam_list",
        semester_id=semester_id,
    )


def _jw_schedule_expected_chunk_count_key(semester_id: str) -> str:
    return f"{JW_SCHEDULE_EXPECTED_CHUNK_COUNT_KEY_PREFIX}{semester_id}"


def _catalog_lesson_chunk_count(
    store: SQLiteModelStore, semester_id: str
) -> int | None:
    table_exists = store.conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table'
          AND name = 'catalog_teach_lesson_list_for_teach'
        """
    ).fetchone()
    if table_exists is None:
        return None

    fetch = store.conn.execute(
        """
        SELECT id FROM upstream_fetches
        WHERE source = 'catalog_teach_lesson_list_for_teach'
          AND ok = 1
          AND context = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"semester_id={semester_id}",),
    ).fetchone()
    if fetch is None:
        return None

    lesson_count = store.conn.execute(
        """
        SELECT COUNT(*) FROM catalog_teach_lesson_list_for_teach
        WHERE fetch_id = ?
        """,
        (fetch[0],),
    ).fetchone()[0]
    return (int(lesson_count) + JW_SCHEDULE_CHUNK_SIZE - 1) // JW_SCHEDULE_CHUNK_SIZE


def _expected_jw_schedule_chunk_count(
    store: SQLiteModelStore, semester_id: str
) -> int | None:
    row = store.conn.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (_jw_schedule_expected_chunk_count_key(semester_id),),
    ).fetchone()
    recorded_count = None
    if row is not None:
        try:
            recorded_count = int(row[0])
        except ValueError:
            return None
        if recorded_count < 0:
            return None

    catalog_count = _catalog_lesson_chunk_count(store, semester_id)
    if catalog_count is None:
        return recorded_count
    if recorded_count is not None and recorded_count != catalog_count:
        return None
    return catalog_count


def _fetch_context_values(context: str | None) -> dict[str, str]:
    return {
        key: value
        for item in (context or "").split("&")
        if "=" in item
        for key, value in [item.split("=", 1)]
    }


def _has_cached_jw_schedule(store: SQLiteModelStore, semester_id: str) -> bool:
    expected_count = _expected_jw_schedule_chunk_count(store, semester_id)
    if expected_count is None:
        return False

    successful_chunks: set[int] = set()
    fetches = store.conn.execute(
        """
        SELECT ok, context FROM upstream_fetches
        WHERE source = 'jw_ws_schedule_table_datum'
        """
    )
    for ok, context in fetches:
        values = _fetch_context_values(context)
        if values.get("semester_id") != semester_id:
            continue
        if not ok:
            return False
        try:
            chunk_index = int(values["chunk_index"])
        except (KeyError, ValueError):
            return False
        if chunk_index in successful_chunks:
            return False
        successful_chunks.add(chunk_index)

    return successful_chunks == set(range(expected_count))


def _has_cached_source_semester(
    store: SQLiteModelStore, *, source: str, semester_id: str
) -> bool:
    return store.conn.execute(
        """
            SELECT COUNT(*), MIN(ok) FROM upstream_fetches
            WHERE source = ?
              AND context = ?
            """,
        (source, f"semester_id={semester_id}"),
    ).fetchone() == (1, 1)


def _delete_source_fetches(store: SQLiteModelStore, source: str) -> None:
    fetch_ids = [
        row[0]
        for row in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ?",
            (source,),
        )
    ]
    store.delete_fetches(fetch_ids)


def _delete_cached_semester(
    store: SQLiteModelStore, guesses: SQLiteGuessStore, semester_id: str
) -> None:
    fetch_ids = [
        row[0]
        for row in store.conn.execute(
            """
            SELECT id FROM upstream_fetches
            WHERE (
                source = 'catalog_teach_lesson_list_for_teach'
                AND context = ?
            )
            OR (
                source = 'jw_ws_schedule_table_datum'
                AND (context = ? OR context LIKE ?)
            )
            """,
            (
                f"semester_id={semester_id}",
                f"semester_id={semester_id}",
                f"%&semester_id={semester_id}",
            ),
        )
    ]
    store.delete_fetches(fetch_ids)
    guesses.delete_semester(semester_id)


def _register_upstream_tables(store: SQLiteModelStore) -> None:
    for table_name, response_model in UPSTREAM_RESPONSE_MODELS.items():
        store.register_response_model(
            table_name=table_name,
            response_model=response_model,
        )


async def _store_catalog_semesters(
    session: RequestSession,
    store: SQLiteModelStore,
    contracts: ObservedContractCollector,
) -> list[Semester]:
    payload = await fetch_semesters_json(session=session)
    contracts.observe_and_assert_compatible(
        CATALOG_SEMESTERS,
        payload,
        CURRICULUM_UPSTREAM_RESPONSE_MODELS[CATALOG_SEMESTERS],
        fetch_context="request",
    )
    response = TeachSemesterListResponse.model_validate(payload)
    if not response.root:
        raise ValueError("Catalog semester response must be a nonempty list")
    ids = [semester.id for semester in response.root]
    if any(semester_id is None or semester_id <= 0 for semester_id in ids) or len(
        ids
    ) != len(set(ids)):
        raise ValueError("Catalog semester response has invalid or duplicate IDs")
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
    session: RequestSession,
    store: SQLiteModelStore,
    contracts: ObservedContractCollector,
) -> None:
    payload = await fetch_departments_json(session=session)
    contracts.observe_and_assert_compatible(
        CATALOG_DEPARTMENTS,
        payload,
        CURRICULUM_UPSTREAM_RESPONSE_MODELS[CATALOG_DEPARTMENTS],
        fetch_context="request",
    )
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


def _delete_cached_exams(store: SQLiteModelStore, semester_id: str) -> None:
    fetch_ids = [
        row[0]
        for row in store.conn.execute(
            "SELECT id FROM upstream_fetches "
            "WHERE source = 'catalog_teach_exam_list' AND context = ?",
            (f"semester_id={semester_id}",),
        )
    ]
    store.delete_fetches(fetch_ids)


async def _store_catalog_exams(
    *,
    session: RequestSession,
    store: SQLiteModelStore,
    contracts: ObservedContractCollector,
    semester_id: str,
) -> None:
    url = f"{CATALOG_EXAM_URL_PREFIX}/{semester_id}"
    try:
        payload = await fetch_exams_json(
            session=session,
            semester_id=semester_id,
            timeout=CATALOG_EXAM_TIMEOUT_MS,
            transient_retries=0,
        )
    except (httpx.TimeoutException, httpx.HTTPStatusError) as error:
        if isinstance(
            error, httpx.HTTPStatusError
        ) and error.response.status_code not in {502, 504}:
            raise
        _delete_cached_exams(store, semester_id)
        store.record_fetch(
            source="catalog_teach_exam_list",
            method="GET",
            url=url,
            context={"semester_id": semester_id},
            ok=False,
            error=f"{type(error).__name__}: {error}",
        )
        logger.warning(
            "Catalog exams unavailable for semester %s: %s",
            semester_id,
            type(error).__name__,
        )
        return
    contracts.observe_and_assert_compatible(
        CATALOG_EXAMS,
        payload,
        CURRICULUM_UPSTREAM_RESPONSE_MODELS[CATALOG_EXAMS],
        fetch_context=f"semester={semester_id}",
    )
    response = TeachExamListResponse.model_validate(payload)
    if response.root is None:
        raise ValueError("Catalog exam response must be a list")
    _delete_cached_exams(store, semester_id)
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
    contracts: ObservedContractCollector,
    semester_id: str,
    catalog_response: TeachLessonListResponse,
    courses: list[Course],
) -> None:
    chunks = _course_chunks(courses)
    store.put_metadata(
        {
            "jw_schedule_chunk_size": JW_SCHEDULE_CHUNK_SIZE,
            _jw_schedule_expected_chunk_count_key(semester_id): len(chunks),
        }
    )

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
            logger.error(
                "Aborting JW schedule table fetch for semester %s after non-JSON "
                "response at chunk %s",
                semester_id,
                chunk_index,
            )
            raise

        contracts.observe_and_assert_compatible(
            JW_SCHEDULES,
            payload,
            CURRICULUM_UPSTREAM_RESPONSE_MODELS[JW_SCHEDULES],
            fetch_context=f"semester={semester_id};chunk={chunk_index}",
            coverage_context=f"semester={semester_id}",
        )
        response = JwWsScheduleTableDatumResponse.model_validate(payload)
        requested_ids = {course.id for course in chunk}
        if response.result is None or response.result.lessonList is None:
            raise ValueError(f"JW response has no lesson list for {semester_id}")
        returned_ids = [lesson.id for lesson in response.result.lessonList]
        if (
            len(returned_ids) != len(requested_ids)
            or set(returned_ids) != requested_ids
        ):
            raise ValueError(
                f"JW lesson IDs do not match requested chunk {chunk_index} "
                f"for semester {semester_id}"
            )
        for rows in (response.result.scheduleList, response.result.scheduleGroupList):
            if rows is None or any(row.lessonId not in requested_ids for row in rows):
                raise ValueError(
                    f"JW schedule/group list is incomplete for {semester_id}"
                )
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

    if not _has_cached_jw_schedule(store, semester_id):
        raise RuntimeError(
            f"Incomplete JW schedule table chunks for semester {semester_id}"
        )
    guesses.add_teacher_section_guesses(
        semester_id=semester_id,
        catalog_lessons=catalog_response,
        jw_schedules=schedule_responses,
    )


def _record_unavailable_curriculum(
    store: SQLiteModelStore,
    guesses: SQLiteGuessStore,
    semester_id: str,
    *,
    source: str,
    error: Exception,
) -> None:
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code not in {
        502,
        504,
    }:
        raise error
    _delete_cached_semester(store, guesses, semester_id)
    store.conn.execute(
        "DELETE FROM metadata WHERE key = ?",
        (_jw_schedule_expected_chunk_count_key(semester_id),),
    )
    is_catalog = source == "catalog_teach_lesson_list_for_teach"
    store.record_fetch(
        source=source,
        method="GET" if is_catalog else "POST",
        url=f"{CATALOG_LESSON_URL_PREFIX}/{semester_id}"
        if is_catalog
        else JW_SCHEDULE_TABLE_URL,
        context={"semester_id": semester_id},
        ok=False,
        error=f"{type(error).__name__}: {error}",
    )
    logger.warning(
        "Curriculum unavailable for semester %s (%s): %s",
        semester_id,
        source,
        type(error).__name__,
    )


async def _store_semester(
    *,
    session: RequestSession,
    store: SQLiteModelStore,
    guesses: SQLiteGuessStore,
    contracts: ObservedContractCollector,
    semester_id: str,
    previous_course_ids_by_code: dict[str, int],
) -> None:
    try:
        payload = await fetch_courses_json(session=session, semester_id=semester_id)
    except (httpx.TimeoutException, httpx.HTTPStatusError) as error:
        _record_unavailable_curriculum(
            store,
            guesses,
            semester_id,
            source="catalog_teach_lesson_list_for_teach",
            error=error,
        )
        return
    contracts.observe_and_assert_compatible(
        CATALOG_LESSONS,
        payload,
        CURRICULUM_UPSTREAM_RESPONSE_MODELS[CATALOG_LESSONS],
        fetch_context=f"semester={semester_id}",
    )
    catalog_response = TeachLessonListResponse.model_validate(payload)
    _course_ids_by_code_from_response(
        catalog_response,
        previous_course_ids_by_code=previous_course_ids_by_code,
    )
    _delete_cached_semester(store, guesses, semester_id)
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
    try:
        await _store_jw_schedule_chunks(
            session=session,
            store=store,
            guesses=guesses,
            contracts=contracts,
            semester_id=semester_id,
            catalog_response=catalog_response,
            courses=courses,
        )
    except (httpx.TimeoutException, httpx.HTTPStatusError) as error:
        _record_unavailable_curriculum(
            store,
            guesses,
            semester_id,
            source="jw_ws_schedule_table_datum",
            error=error,
        )


async def _collect_semesters(
    semesters: list[Semester],
    collect: Callable[[Semester], Awaitable[None]],
    *,
    description: str,
) -> None:
    semaphore = asyncio.Semaphore(CURRICULUM_FETCH_CONCURRENCY)
    with tqdm(
        total=len(semesters), position=1, leave=True, desc=description
    ) as progress:

        async def collect_one(semester: Semester) -> None:
            async with semaphore:
                await collect(semester)
                progress.update(1)

        # Drain cancelled requests before a fatal error rolls back SQLite.
        async with asyncio.TaskGroup() as tasks:
            for semester in semesters:
                tasks.create_task(collect_one(semester))


async def make_curriculum(*, verify_upstream_contract: bool = False) -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = BUILD_DIR / SNAPSHOT_FILENAME
    guesses_path = BUILD_DIR / GUESSES_FILENAME
    reuse_snapshot = snapshot_path.exists()
    reuse_guesses = guesses_path.exists()
    store = SQLiteModelStore(snapshot_path, reset=not reuse_snapshot)
    guesses = SQLiteGuessStore(guesses_path, reset=not reuse_guesses)
    contracts = ObservedContractCollector()
    contracts.require_contexts(CATALOG_SEMESTERS, {"request"})
    contracts.require_contexts(CATALOG_DEPARTMENTS, {"request"})

    try:
        _register_upstream_tables(store)
        previous_course_ids_by_code = _stored_course_ids_by_code(store)
        async with USTCSession() as session:
            _delete_source_fetches(store, "catalog_teach_semester_list")
            semesters = await _store_catalog_semesters(
                session=session, store=store, contracts=contracts
            )
            _delete_source_fetches(store, "catalog_teach_department_college_tree")
            await _store_catalog_departments(
                session=session, store=store, contracts=contracts
            )
            selected_semesters = semesters
            selected_semester_ids = {
                f"semester={semester.id}" for semester in selected_semesters
            }
            contracts.require_contexts(CATALOG_LESSONS, selected_semester_ids)
            contracts.require_contexts(CATALOG_EXAMS, selected_semester_ids)
            reuse_curriculum_cache = reuse_snapshot and reuse_guesses
            now_timestamp = time.time()
            cached_semester_ids = (
                _cached_fresh_lesson_semester_ids(
                    store, selected_semesters, now_timestamp=now_timestamp
                )
                if reuse_curriculum_cache
                else set()
            )
            refreshed_semesters = _curriculum_semesters_to_refresh(
                selected_semesters,
                cached_semester_ids=cached_semester_ids,
                now_timestamp=now_timestamp,
                verify_upstream_contract=verify_upstream_contract,
            )
            cached_exam_semester_ids = (
                _cached_fresh_exam_semester_ids(
                    store, selected_semesters, now_timestamp=now_timestamp
                )
                if reuse_curriculum_cache
                else set()
            )
            refreshed_exam_semesters = _curriculum_semesters_to_refresh(
                selected_semesters,
                cached_semester_ids=cached_exam_semester_ids,
                now_timestamp=now_timestamp,
                verify_upstream_contract=verify_upstream_contract,
            )
            refreshed_exam_ids = {
                str(semester.id) for semester in refreshed_exam_semesters
            }
            refreshed_semester_ids = {
                str(semester.id) for semester in refreshed_semesters
            }
            cached_ended_semester_ids = [
                str(semester.id)
                for semester in sorted(selected_semesters, key=_semester_sort_key)
                if str(semester.id) not in refreshed_semester_ids
            ]
            store.put_metadata(
                {
                    "curriculum_mode": "contract_verification"
                    if verify_upstream_contract
                    else "incremental"
                    if reuse_curriculum_cache
                    else "all",
                    "curriculum_cache_source": "previous_artifact"
                    if reuse_curriculum_cache
                    else "none",
                    "discovered_semester_count": len(semesters),
                    "selected_semester_count": len(selected_semesters),
                    "refreshed_semester_count": len(refreshed_semesters),
                    "cached_ended_semester_count": len(cached_ended_semester_ids),
                    "cached_ended_semester_ids": ",".join(cached_ended_semester_ids),
                    "selected_semester_ids": ",".join(
                        str(semester.id)
                        for semester in sorted(semesters, key=_semester_sort_key)
                    ),
                    "refreshed_semester_ids": ",".join(
                        sorted(refreshed_semester_ids, key=int)
                    ),
                    "ended_semester_cache_max_age_seconds": (
                        ENDED_SEMESTER_CACHE_MAX_AGE_SECONDS
                    ),
                    "catalog_lesson_min_semester_id": 1,
                    "catalog_exam_min_semester_id": 1,
                    "catalog_exam_selected_semester_count": len(selected_semesters),
                    "catalog_lesson_skipped_legacy_semester_count": 0,
                    "catalog_lesson_skipped_legacy_semester_ids": "",
                    "catalog_exam_skipped_legacy_semester_count": 0,
                    "catalog_exam_skipped_legacy_semester_ids": "",
                    "jw_schedule_min_semester_id": 1,
                    "jw_schedule_selected_semester_count": len(selected_semesters),
                    "jw_schedule_skipped_legacy_semester_count": 0,
                    "catalog_exam_refreshed_semester_ids": ",".join(
                        sorted(refreshed_exam_ids, key=int)
                    ),
                    "catalog_exam_cached_semester_ids": ",".join(
                        sorted(
                            {str(semester.id) for semester in selected_semesters}
                            - refreshed_exam_ids,
                            key=int,
                        )
                    ),
                    "curriculum_fetch_status": "in_progress",
                }
            )

            logger.info(
                "Discovered %s semester(s); refreshing %s selected semester(s); "
                "using cached data for %s ended semester(s)",
                len(semesters),
                len(refreshed_semesters),
                len(cached_ended_semester_ids),
            )

            async def collect_curriculum(semester: Semester) -> None:
                await _store_semester(
                    session=session,
                    store=store,
                    guesses=guesses,
                    contracts=contracts,
                    semester_id=str(semester.id),
                    previous_course_ids_by_code=previous_course_ids_by_code,
                )

            await _collect_semesters(
                refreshed_semesters,
                collect_curriculum,
                description="Processing semesters",
            )
            _stored_course_ids_by_code(store)
            complete_ids = _cached_fresh_lesson_semester_ids(
                store, selected_semesters, now_timestamp=time.time()
            )
            unavailable_ids = {
                _fetch_context_values(context)["semester_id"]
                for (context,) in store.conn.execute(
                    "SELECT context FROM upstream_fetches WHERE ok = 0 "
                    "AND source IN ('catalog_teach_lesson_list_for_teach', "
                    "'jw_ws_schedule_table_datum')"
                )
            }
            if (
                unavailable_ids
                != {str(semester.id) for semester in selected_semesters} - complete_ids
            ):
                raise RuntimeError(
                    "Curriculum missing explicit success/failure provenance"
                )

            async def collect_exams(semester: Semester) -> None:
                await _store_catalog_exams(
                    session=session,
                    store=store,
                    contracts=contracts,
                    semester_id=str(semester.id),
                )

            await _collect_semesters(
                refreshed_exam_semesters, collect_exams, description="Processing exams"
            )
            exam_success_ids = {
                str(semester.id)
                for semester in selected_semesters
                if _has_cached_catalog_exams(store, str(semester.id))
            }
            exam_failed_ids = {
                _fetch_context_values(context)["semester_id"]
                for (context,) in store.conn.execute(
                    "SELECT context FROM upstream_fetches "
                    "WHERE source = 'catalog_teach_exam_list' AND ok = 0"
                )
            }
            if (
                exam_failed_ids
                != {str(semester.id) for semester in selected_semesters}
                - exam_success_ids
            ):
                raise RuntimeError(
                    "Catalog exams missing explicit success/failure provenance"
                )
            store.put_metadata(
                {
                    "curriculum_fetch_status": "complete"
                    if not (exam_failed_ids or unavailable_ids)
                    else "partial_sources_unavailable",
                    "curriculum_successful_semester_ids": ",".join(
                        sorted(complete_ids, key=int)
                    ),
                    "curriculum_unavailable_semester_ids": ",".join(
                        sorted(unavailable_ids, key=int)
                    ),
                    "catalog_exam_successful_semester_ids": ",".join(
                        sorted(exam_success_ids, key=int)
                    ),
                    "catalog_exam_unavailable_semester_ids": ",".join(
                        sorted(exam_failed_ids, key=int)
                    ),
                }
            )
            contracts.require_contexts(
                JW_SCHEDULES,
                {
                    f"semester={semester.id}"
                    for semester in selected_semesters
                    if _catalog_lesson_chunk_count(store, str(semester.id))
                },
            )
            diagnostic_dir = BASE_DIR / ".artifacts" / "upstream-contracts"
            issues = publish_contract_artifacts(
                contracts,
                BUILD_DIR / "schemas" / "upstream",
                diagnostic_dir,
            )
            log_contract_diagnostics(
                issues,
                diagnostic_dir / "contract-report.json",
                logger=logger,
            )
            store.put_metadata({"generated_at": datetime.now(UTC).isoformat()})
    except BaseException:
        store.conn.rollback()
        guesses.conn.rollback()
        raise
    finally:
        store.close()
        guesses.close()
