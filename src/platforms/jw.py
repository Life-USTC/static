import json
import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import httpx
from bs4 import BeautifulSoup

from src.batch import fetch_in_batches, refresh_due
from src.http import RequestSession
from src.models.api.catalog_api_teach_lesson_list_for_teach import TeachLessonListItem
from src.models.api.jw_for_std_lesson_search_semester import FieldPage, RoomType
from src.sqlite_store import SQLiteModelStore

_jw_user_id_cache: WeakKeyDictionary[RequestSession, str] = WeakKeyDictionary()


async def _get_jw_user_id(session: RequestSession) -> str:
    if user_id := _jw_user_id_cache.get(session):
        return user_id

    response = await session.get("https://jw.ustc.edu.cn/for-std/course-select")
    user_id = urlsplit(response.url).path.rstrip("/").rsplit("/", 1)[-1]
    if not user_id.isnumeric():
        raise ValueError("JW session is not authenticated")
    _jw_user_id_cache[session] = user_id
    return user_id


async def fetch_jw_courses_json(session: RequestSession, semester_id: str) -> dict:
    user_id = await _get_jw_user_id(session=session)
    url = f"https://jw.ustc.edu.cn/for-std/lesson-search/semester/{semester_id}/search/{user_id}"
    headers = {
        "accept": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "referer": f"https://jw.ustc.edu.cn/for-std/lesson-search/index/{user_id}",
    }

    async def fetch(page: int):
        result = await session.get_json(
            f"{url}?queryPage__={page}%2C200", headers=headers, timeout=60_000
        )
        info = FieldPage.model_validate(result.get("_page_"))
        rows = result.get("data")
        if (
            not isinstance(rows, list)
            or info.currentPage != page
            or info.rowsInPage != len(rows)
            or info.rowsPerPage != 200
            or info.totalRows is None
            or info.totalRows < 0
            or info.totalPages != (info.totalRows + 199) // 200
        ):
            raise ValueError("Invalid JW lesson-search pagination")
        return result

    first = await fetch(1)
    pages = await fetch_in_batches(
        fetch, range(2, first["_page_"]["totalPages"] + 1), 2, 0.2
    )
    rows = [row for page in [first, *pages] for row in page["data"]]
    total = first["_page_"]["totalRows"]
    if (
        any(page["_page_"]["totalRows"] != total for page in pages)
        or len(rows) != total
        or len({row["id"] for row in rows}) != total
    ):
        raise ValueError("JW lesson-search changed during pagination")
    # Preserve the complete-result contract used by the dictionary collector.
    return {
        "data": rows,
        "_page_": {
            "currentPage": 1,
            "rowsInPage": total,
            "rowsPerPage": total,
            "totalRows": total,
            "totalPages": int(bool(total)),
        },
    }


async def fetch_jw_semesters(session: RequestSession) -> list[tuple[str, str]]:
    user_id = await _get_jw_user_id(session)
    response = await session.get(
        f"https://jw.ustc.edu.cn/for-std/lesson-search/index/{user_id}"
    )
    options = BeautifulSoup(await response.text(), "html.parser").select(
        "select#semester option"
    )
    terms = [
        (str(option.get("value", "")), option.get_text(strip=True))
        for option in options
    ]
    if not terms or any(not id_.isdecimal() or not name for id_, name in terms):
        raise ValueError("JW returned no valid semester list")
    if len({id_ for id_, _ in terms}) != len(terms):
        raise ValueError("JW returned duplicate semester IDs")
    return terms


async def fetch_jw_schedule_table_json(
    session: RequestSession, course_list: list[TeachLessonListItem]
) -> dict:
    url = "https://jw.ustc.edu.cn/ws/schedule-table/datum"
    course_id_list = [str(course.id) for course in course_list]
    return await session.post_json(
        url=url,
        data={"lessonIds": course_id_list},
        timeout=30_000,
        transient_retries=2,
    )


ROOM_TYPE_SOURCE = "jw_lesson_search_room_types"


def _parse_room_types(payload: dict) -> list[RoomType]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("JW lesson-search must return a data list")
    page = FieldPage.model_validate(payload.get("_page_"))
    if page.currentPage != 1 or page.totalRows != len(payload["data"]):
        raise ValueError("Incomplete JW lesson-search page for room types")
    room_types: dict[int, RoomType] = {}
    for lesson in payload["data"]:
        if not isinstance(lesson, dict) or "roomType" not in lesson:
            raise ValueError("JW lesson-search lesson has no roomType field")
        if lesson["roomType"] is None:
            continue
        room_type = RoomType.model_validate(lesson["roomType"])
        if (
            room_type.id is None
            or room_type.id <= 0
            or not (room_type.code or "").strip()
            or not (room_type.nameZh or "").strip()
        ):
            raise ValueError("JW room type has no valid ID, code or name")
        previous = room_types.get(room_type.id)
        if previous is not None and (
            previous.code,
            previous.nameZh,
            previous.nameEn,
        ) != (room_type.code, room_type.nameZh, room_type.nameEn):
            raise ValueError(f"Conflicting JW room type {room_type.id}")
        room_types[room_type.id] = room_type
    return list(room_types.values())


def _missing_room_type_semesters(store: SQLiteModelStore) -> dict[int, set[str]]:
    rows = store.conn.execute(
        """
        SELECT DISTINCT lesson.roomTypeId, fetched.context
        FROM jw_ws_schedule_table_datum_result_lessonList AS lesson
        JOIN upstream_fetches AS fetched ON fetched.id = lesson.fetch_id
        WHERE lesson.roomTypeId IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM jw_room_types AS known
                          WHERE known.id = lesson.roomTypeId)
          AND NOT EXISTS (
            SELECT 1
            FROM jw_ws_schedule_table_datum_result_scheduleList_room_roomType AS known
            WHERE known.id = lesson.roomTypeId
              AND TRIM(COALESCE(known.code, '')) != ''
              AND TRIM(COALESCE(known.nameZh, '')) != ''
          )
        """
    )
    result: dict[int, set[str]] = {}
    for room_type_id, context in rows:
        values = dict(item.split("=", 1) for item in context.split("&"))
        result.setdefault(room_type_id, set()).add(values["semester_id"])
    return result


async def ensure_room_types(
    *, session: RequestSession, store: SQLiteModelStore
) -> None:
    """Refresh the small supplemental dictionary from current authoritative objects."""
    previous_fetches = [
        row[0]
        for row in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ?", (ROOM_TYPE_SOURCE,)
        )
    ]
    store.delete_fetches(previous_fetches)
    attempted: set[str] = set()
    while missing := _missing_room_type_semesters(store):
        candidates = set().union(*missing.values()) - attempted
        if not candidates:
            raise ValueError(f"Unresolved JW room types: {sorted(missing)}")
        semester_id = max(
            candidates,
            key=lambda sid: (sum(sid in ids for ids in missing.values()), int(sid)),
        )
        attempted.add(semester_id)
        # The authenticated account path is deliberately not copied into public data.
        url = f"https://jw.ustc.edu.cn/for-std/lesson-search/semester/{semester_id}"
        context = {"semester_id": semester_id}
        try:
            payload = await fetch_jw_courses_json(session, semester_id)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.HTTPStatusError,
        ) as error:
            if isinstance(
                error, httpx.HTTPStatusError
            ) and error.response.status_code not in {502, 504}:
                raise
            store.record_fetch(
                source=ROOM_TYPE_SOURCE,
                method="GET",
                url=url,
                context=context,
                ok=False,
                error=(
                    f"HTTP {error.response.status_code}"
                    if isinstance(error, httpx.HTTPStatusError)
                    else type(error).__name__
                ),
            )
            continue
        room_types = _parse_room_types(payload)
        fetch_id = store.record_fetch(
            source=ROOM_TYPE_SOURCE, method="GET", url=url, context=context
        )
        store.conn.executemany(
            """
            INSERT INTO jw_room_types(id, semester_id, code, nameZh, nameEn, fetch_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, semester_id) DO UPDATE SET
              code=excluded.code, nameZh=excluded.nameZh, nameEn=excluded.nameEn,
              fetch_id=excluded.fetch_id
            """,
            [
                (item.id, semester_id, item.code, item.nameZh, item.nameEn, fetch_id)
                for item in room_types
            ],
        )
    store.put_metadata({"jw_room_types_status": "complete"})


LOGGER = logging.getLogger(__name__)
COURSES = "jw_catalog_course_infos"
LESSONS = "jw_catalog_lesson_infos"


def _name(value):
    return (value or {}).get("nameZh") or (value or {}).get("name")


def _profile(course, lesson=None):
    """Use the existing profile vocabulary; never publish teacher/account objects."""
    source = lesson or course
    periods = source.get("requiredPeriodInfo") if lesson else course.get("periodInfo")
    profile = {
        "id": source["id"],
        "code": source["code"],
        "name": {"cn": course.get("nameZh"), "en": course.get("nameEn")},
        "desc": {"cn": course.get("introduction"), "en": course.get("introductionEn")},
        "credit": source.get("credits"),
        "hour": (periods or {}).get("total"),
        "dept": _name(
            source.get("openDepartment") if lesson else course.get("defaultOpenDepart")
        ),
        "courseType": _name(source.get("courseType")),
        "courseCategory": _name(source.get("courseCategory")),
        "courseClassify": _name(source.get("courseClassify")),
        "discipline": _name(course.get("disciplineType")),
        "grading": _name(course.get("scoreMarkStyle")),
        "examType": _name(
            source.get("examMode") if lesson else course.get("defaultExamMode")
        ),
        "lang": _name(source.get("teachLang")),
        "textbook": lesson.get("textbook")
        if lesson
        else course.get("designateTextbook"),
        "ref": course.get("referenceTextbook"),
        "sem": ";".join(
            {"SPRING": "春", "SUMMER": "夏", "AUTUMN": "秋", "WINTER": "冬"}.get(
                value, value
            )
            for value in course.get("seasons") or []
        ),
        "preq": lesson.get("preCourseText")
        if lesson
        else ";".join(
            item.get("nameZh") or item.get("code") or ""
            for item in course.get("defaultPreCourses") or []
        ),
        "valid": course.get("enabled", True),
        "syllabus": None,
        "textbooks": None,
        "lastTerm": _name(course.get("lastExecuteSemester")),
    }
    if lesson:
        profile["courseId"] = course["id"]
    return profile


def _write_profiles(store, table, rows, fetch_id, semester_id):
    store.conn.executemany(
        f"INSERT INTO {table}(id,code,payload_json,semester_id,fetch_id,courseId) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET code=excluded.code, "
        "payload_json=excluded.payload_json, "
        "semester_id=excluded.semester_id, fetch_id=excluded.fetch_id, "
        "courseId=excluded.courseId",
        [
            (
                row["id"],
                row["code"],
                json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                semester_id,
                fetch_id,
                row.get("courseId"),
            )
            for row in rows
        ],
    )


async def refresh_jw_profiles(session: RequestSession, store: SQLiteModelStore):
    now = datetime.now(UTC)
    terms = await fetch_jw_semesters(session)
    for table in (COURSES, LESSONS):
        store.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table}(id INTEGER PRIMARY KEY, "
            "code TEXT NOT NULL, courseId INTEGER, "
            "payload_json TEXT NOT NULL, semester_id TEXT NOT NULL, "
            "fetch_id INTEGER NOT NULL REFERENCES upstream_fetches(id) "
            "ON DELETE CASCADE)"
        )
    # Oldest first: shared course defaults are taken from the latest offering.
    for semester_id, name in reversed(terms):
        key = f"jw_catalog_semester_{semester_id}"
        saved = store.conn.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        previous = json.loads(saved[0]) if saved else {}
        timestamp = previous.get("fetched_at")
        if timestamp and (
            timestamp[:10] == now.date().isoformat()
            or previous.get("ended")
            and not refresh_due(key, timestamp, now)
        ):
            continue
        payload = await fetch_jw_courses_json(session, semester_id)
        rows = payload["data"]
        courses = {}
        lessons = []
        for row in rows:
            course = row["course"]
            for item in (row, course):
                if (
                    type(item.get("id")) is not int
                    or item["id"] <= 0
                    or not isinstance(item.get("code"), str)
                    or not item["code"].strip()
                ):
                    raise ValueError("JW returned an invalid lesson/course identity")
            if row.get("semester", {}).get("id") != int(semester_id):
                raise ValueError("JW lesson belongs to a different semester")
            courses[course["id"]] = _profile(course)
            lessons.append(_profile(course, row))
        end = (rows[0].get("semester") or {}).get("endDate") if rows else None
        ended = end < now.date().isoformat() if end else int(name[:4]) < now.year
        with store.conn:
            # A failed page leaves this semester's previous snapshot untouched.
            ids = [
                r[0]
                for r in store.conn.execute(
                    "SELECT id FROM upstream_fetches WHERE source=? AND context=?",
                    (LESSONS, f"semester_id={semester_id}"),
                )
            ]
            store.delete_fetches(ids)
            fetch_id = store.record_fetch(
                source=LESSONS,
                method="GET",
                url=f"https://jw.ustc.edu.cn/for-std/lesson-search/semester/{semester_id}",
                context={"semester_id": semester_id},
            )
            _write_profiles(store, COURSES, courses.values(), fetch_id, semester_id)
            _write_profiles(store, LESSONS, lessons, fetch_id, semester_id)
            store.put_metadata(
                {
                    key: json.dumps(
                        {"fetched_at": now.isoformat(), "ended": ended, "name": name}
                    )
                }
            )
        LOGGER.info("JW %s: %s lessons, %s courses", name, len(rows), len(courses))
    store.put_metadata(
        {
            "jw_catalog_updated_at": now.isoformat(),
            "jw_catalog_semester_count": len(terms),
        }
    )
