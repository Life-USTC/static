import logging
import time
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
CATALOG_LESSON_URL_PREFIX = (
    "https://catalog.ustc.edu.cn/api/teach/lesson/list-for-teach"
)
CATALOG_EXAM_URL_PREFIX = "https://catalog.ustc.edu.cn/api/teach/exam/list"
JW_SCHEDULE_TABLE_URL = "https://jw.ustc.edu.cn/ws/schedule-table/datum"
MIN_CATALOG_LESSON_SEMESTER_ID = 221
MIN_CATALOG_EXAM_SEMESTER_ID = 381
JW_SCHEDULE_CHUNK_SIZE = 100
JW_SCHEDULE_EXPECTED_CHUNK_COUNT_KEY_PREFIX = "jw_schedule_expected_chunk_count_"


def _course_chunks(
    courses: list[Course], chunk_size: int = JW_SCHEDULE_CHUNK_SIZE
) -> list[list[Course]]:
    return [courses[i : i + chunk_size] for i in range(0, len(courses), chunk_size)]


def _is_semester_at_or_after(semester_id: str, minimum_semester_id: int) -> bool:
    try:
        return int(semester_id) >= minimum_semester_id
    except ValueError:
        return True


def _should_fetch_catalog_lessons(semester_id: str) -> bool:
    return _is_semester_at_or_after(semester_id, MIN_CATALOG_LESSON_SEMESTER_ID)


def _should_fetch_jw_schedule_table(semester_id: str) -> bool:
    return True


def _should_fetch_catalog_exams(semester_id: str) -> bool:
    return _is_semester_at_or_after(semester_id, MIN_CATALOG_EXAM_SEMESTER_ID)


def _semester_sort_key(semester: Semester) -> int:
    try:
        return int(semester.id)
    except ValueError:
        return 0


def _selected_curriculum_semesters(semesters: list[Semester]) -> list[Semester]:
    return [
        semester
        for semester in semesters
        if _should_fetch_catalog_lessons(str(semester.id))
    ]


def _semester_has_ended(semester: Semester, now_timestamp: int) -> bool:
    return semester.endDate > 0 and semester.endDate < now_timestamp


def _refresh_curriculum_semesters(
    semesters: list[Semester],
    *,
    cached_semester_ids: set[str],
    now_timestamp: int,
) -> list[Semester]:
    return [
        semester
        for semester in semesters
        if not _semester_has_ended(semester, now_timestamp)
        or str(semester.id) not in cached_semester_ids
    ]


def _cached_complete_semester_ids(
    store: SQLiteModelStore, semesters: list[Semester]
) -> set[str]:
    return {
        str(semester.id)
        for semester in semesters
        if _has_cached_catalog_lessons(store, str(semester.id))
        and _has_cached_jw_schedule(store, str(semester.id))
        and (
            not _should_fetch_catalog_exams(str(semester.id))
            or _has_cached_catalog_exams(store, str(semester.id))
        )
    }


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
    return (
        store.conn.execute(
            """
            SELECT 1 FROM upstream_fetches
            WHERE source = ?
              AND ok = 1
              AND context = ?
            LIMIT 1
            """,
            (source, f"semester_id={semester_id}"),
        ).fetchone()
        is not None
    )


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
                source IN (
                    'catalog_teach_lesson_list_for_teach',
                    'catalog_teach_exam_list'
                )
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


def _is_skippable_exam_fetch_error(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {502, 504}

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
    if not _should_fetch_catalog_exams(semester_id):
        logger.info(
            "Skipping catalog exams for legacy semester %s below minimum id %s",
            semester_id,
            MIN_CATALOG_EXAM_SEMESTER_ID,
        )
        return

    try:
        payload = await fetch_exams_json(
            session=session,
            semester_id=semester_id,
            transient_retries=0,
        )
    except Exception as e:
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

    if not _has_cached_jw_schedule(store, semester_id):
        raise RuntimeError(
            f"Incomplete JW schedule table chunks for semester {semester_id}"
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
    snapshot_path = BUILD_DIR / SNAPSHOT_FILENAME
    guesses_path = BUILD_DIR / GUESSES_FILENAME
    reuse_snapshot = snapshot_path.exists()
    reuse_guesses = guesses_path.exists()
    store = SQLiteModelStore(snapshot_path, reset=not reuse_snapshot)
    guesses = SQLiteGuessStore(guesses_path, reset=not reuse_guesses)

    try:
        _register_upstream_tables(store)
        async with USTCSession() as session:
            _delete_source_fetches(store, "catalog_teach_semester_list")
            semesters = await _store_catalog_semesters(session=session, store=store)
            _delete_source_fetches(store, "catalog_teach_department_college_tree")
            await _store_catalog_departments(session=session, store=store)
            selected_semesters = _selected_curriculum_semesters(semesters)
            reuse_curriculum_cache = reuse_snapshot and reuse_guesses
            cached_semester_ids = (
                _cached_complete_semester_ids(store, selected_semesters)
                if reuse_curriculum_cache
                else set()
            )
            now_timestamp = int(time.time())
            refreshed_semesters = _refresh_curriculum_semesters(
                selected_semesters,
                cached_semester_ids=cached_semester_ids,
                now_timestamp=now_timestamp,
            )
            refreshed_semester_ids = {
                str(semester.id) for semester in refreshed_semesters
            }
            cached_ended_semester_ids = [
                str(semester.id)
                for semester in sorted(selected_semesters, key=_semester_sort_key)
                if str(semester.id) not in refreshed_semester_ids
            ]
            skipped_catalog_lesson_semester_ids = [
                str(semester.id)
                for semester in sorted(semesters, key=_semester_sort_key)
                if not _should_fetch_catalog_lessons(str(semester.id))
            ]
            skipped_catalog_exam_semester_ids = [
                str(semester.id)
                for semester in sorted(selected_semesters, key=_semester_sort_key)
                if not _should_fetch_catalog_exams(str(semester.id))
            ]
            store.put_metadata(
                {
                    "curriculum_mode": "incremental"
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
                    "catalog_lesson_min_semester_id": MIN_CATALOG_LESSON_SEMESTER_ID,
                    "catalog_lesson_skipped_legacy_semester_count": len(
                        skipped_catalog_lesson_semester_ids
                    ),
                    "catalog_lesson_skipped_legacy_semester_ids": ",".join(
                        skipped_catalog_lesson_semester_ids
                    ),
                    "jw_schedule_min_semester_id": "selected_catalog_lessons",
                    "jw_schedule_selected_semester_count": sum(
                        _should_fetch_jw_schedule_table(str(semester.id))
                        for semester in selected_semesters
                    ),
                    "jw_schedule_skipped_legacy_semester_count": sum(
                        not _should_fetch_jw_schedule_table(str(semester.id))
                        for semester in selected_semesters
                    ),
                    "catalog_exam_min_semester_id": MIN_CATALOG_EXAM_SEMESTER_ID,
                    "catalog_exam_selected_semester_count": sum(
                        _should_fetch_catalog_exams(str(semester.id))
                        for semester in selected_semesters
                    ),
                    "catalog_exam_skipped_legacy_semester_count": len(
                        skipped_catalog_exam_semester_ids
                    ),
                    "catalog_exam_skipped_legacy_semester_ids": ",".join(
                        skipped_catalog_exam_semester_ids
                    ),
                }
            )

            logger.info(
                "Discovered %s semester(s); refreshing %s selected semester(s); "
                "using cached data for %s ended semester(s)",
                len(semesters),
                len(refreshed_semesters),
                len(cached_ended_semester_ids),
            )

            for semester in tqdm(
                refreshed_semesters,
                position=1,
                leave=True,
                desc="Processing semesters",
            ):
                _delete_cached_semester(store, guesses, str(semester.id))
                await _store_semester(
                    session=session,
                    store=store,
                    guesses=guesses,
                    semester_id=str(semester.id),
                )
    finally:
        store.close()
        guesses.close()
