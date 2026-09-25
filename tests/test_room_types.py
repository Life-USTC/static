import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.models.api.jw_for_std_lesson_search_semester import RoomType
from src.room_types import _parse_room_types, ensure_room_types
from src.sqlite_store import SCHEMA_VERSION, SQLiteModelStore


def _payload(*ids: int) -> dict:
    return {
        "data": [
            {
                "roomType": dict.fromkeys(RoomType.model_fields)
                | {"id": i, "code": str(i), "nameZh": f"教室类型{i}"}
            }
            for i in ids
        ],
        "_page_": {
            "currentPage": 1,
            "rowsInPage": len(ids),
            "rowsPerPage": 100000,
            "totalRows": len(ids),
            "totalPages": 1,
        },
    }


def _store() -> SQLiteModelStore:
    store = SQLiteModelStore(":memory:")
    store.conn.executescript("""
        CREATE TABLE jw_ws_schedule_table_datum_result_lessonList (
            roomTypeId INTEGER, fetch_id INTEGER
        );
        CREATE TABLE jw_ws_schedule_table_datum_result_scheduleList_room_roomType (
            id INTEGER, code TEXT, nameZh TEXT
        );
    """)
    for room_id, semester_id in [(23, "221"), (29, "241")]:
        fetch_id = store.record_fetch(
            source="jw_ws_schedule_table_datum",
            method="POST",
            url="jw",
            context={"semester_id": semester_id, "chunk_index": 0},
        )
        store.conn.execute(
            "INSERT INTO jw_ws_schedule_table_datum_result_lessonList VALUES (?, ?)",
            (room_id, fetch_id),
        )
    return store


class RoomTypeTest(unittest.IsolatedAsyncioTestCase):
    async def test_dictionary_is_always_part_of_schema_six(self):
        store = SQLiteModelStore(":memory:")
        try:
            self.assertEqual(SCHEMA_VERSION, 6)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM jw_room_types").fetchone()[0],
                0,
            )
        finally:
            store.close()

    async def test_refreshes_missing_dictionary_from_authoritative_objects(self):
        store = _store()
        try:

            async def fetch(session, semester_id):
                return _payload(29) if semester_id == "241" else _payload(23, 30)

            with patch(
                "src.room_types.fetch_jw_courses_json", AsyncMock(side_effect=fetch)
            ) as request:
                await ensure_room_types(session=MagicMock(), store=store)
                self.assertEqual(request.await_count, 2)
                self.assertEqual(
                    store.conn.execute(
                        "SELECT id,semester_id FROM jw_room_types ORDER BY id"
                    ).fetchall(),
                    [(23, "221"), (29, "241"), (30, "221")],
                )
                request.reset_mock()
                await ensure_room_types(session=MagicMock(), store=store)
                self.assertEqual(request.await_count, 2)
        finally:
            store.close()

    async def test_existing_schedule_dictionary_does_not_need_network(self):
        store = _store()
        try:
            store.conn.executemany(
                "INSERT INTO "
                "jw_ws_schedule_table_datum_result_scheduleList_room_roomType "
                "VALUES (?, ?, ?)",
                [(23, "23", "普通教室"), (29, "29", "其他教室")],
            )
            with patch("src.room_types.fetch_jw_courses_json", AsyncMock()) as request:
                await ensure_room_types(session=MagicMock(), store=store)
                request.assert_not_awaited()
        finally:
            store.close()

    async def test_network_failure_cannot_turn_missing_dictionary_into_success(self):
        store = _store()
        try:
            with (
                patch(
                    "src.room_types.fetch_jw_courses_json",
                    AsyncMock(side_effect=httpx.ConnectError("unavailable")),
                ),
                self.assertRaisesRegex(ValueError, "Unresolved JW room types"),
            ):
                await ensure_room_types(session=MagicMock(), store=store)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM jw_room_types").fetchone()[0],
                0,
            )
        finally:
            store.close()

    async def test_failure_provenance_does_not_publish_authenticated_account_path(self):
        store = _store()
        url = "https://jw.ustc.edu.cn/for-std/lesson-search/semester/221/search/private-account"
        request = httpx.Request("GET", url)
        error = httpx.HTTPStatusError(
            url, request=request, response=httpx.Response(502, request=request)
        )
        try:
            with (
                patch(
                    "src.room_types.fetch_jw_courses_json", AsyncMock(side_effect=error)
                ),
                self.assertRaises(ValueError),
            ):
                await ensure_room_types(session=MagicMock(), store=store)
            failures = store.conn.execute(
                "SELECT error FROM upstream_fetches WHERE ok=0"
            ).fetchall()
            self.assertEqual(failures, [("HTTP 502",), ("HTTP 502",)])
        finally:
            store.close()

    def test_rejects_truncated_or_conflicting_room_type_responses(self):
        payload = _payload(23)
        payload["_page_"]["totalRows"] = 2
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            _parse_room_types(payload)
        payload = _payload(23, 23)
        payload["data"][1]["roomType"]["nameZh"] = "another room type"
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            _parse_room_types(payload)
