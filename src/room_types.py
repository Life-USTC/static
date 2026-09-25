"""Resolve lesson room-type references from authoritative JW lesson-search objects."""

import httpx

from .models.api.jw_for_std_lesson_search_semester import FieldPage, RoomType
from .sqlite_store import SQLiteModelStore
from .utils.auth import RequestSession
from .utils.jw import fetch_jw_courses_json

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
