import asyncio
import unittest
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.curriculum import (
    ENDED_SEMESTER_CACHE_MAX_AGE_SECONDS,
    _cached_fresh_exam_semester_ids,
    _cached_fresh_lesson_semester_ids,
    _collect_semesters,
    _course_ids_by_code_from_response,
    _curriculum_semesters_to_refresh,
    _has_cached_jw_schedule,
    _jw_schedule_expected_chunk_count_key,
    _refresh_curriculum_semesters,
    _register_upstream_tables,
    _semester_has_ended,
    _store_catalog_exams,
    _store_catalog_semesters,
    _store_jw_schedule_chunks,
    _store_semester,
    _stored_course_ids_by_code,
    make_curriculum,
)
from src.models.api.catalog_api_teach_lesson_list_for_teach import (
    Course,
    TeachLessonListItem,
    TeachLessonListResponse,
)
from src.models.api.jw_ws_schedule_table_datum import LessonListItem
from src.models.semester import Semester
from src.observed_contracts import ObservedContractCollector
from src.sqlite_store import SQLiteModelStore


def _semester(
    semester_id: str,
    *,
    end_date: int = 0,
) -> Semester:
    return Semester(
        id=semester_id,
        courses=[],
        name=f"semester {semester_id}",
        startDate=0,
        endDate=end_date,
    )


def _catalog_lesson(
    lesson_id: int,
    *,
    course_id: int | None,
    course_code: str | None,
    course_cn: str | None = "课程",
) -> TeachLessonListItem:
    values = {name: None for name in TeachLessonListItem.model_fields}
    values["id"] = lesson_id
    if course_id is not None or course_code is not None:
        course_values = {name: None for name in Course.model_fields}
        course_values.update(id=course_id, code=course_code, cn=course_cn)
        values["course"] = Course(**course_values)
    return TeachLessonListItem(**values)


class CatalogCourseIdentityTest(unittest.TestCase):
    def test_preserves_distinct_lesson_and_course_ids(self) -> None:
        response = TeachLessonListResponse(
            root=[_catalog_lesson(181384, course_id=144481, course_code="MATH1001")]
        )
        store = SQLiteModelStore(":memory:")
        try:
            store.register_response_model(
                table_name="catalog_teach_lesson_list_for_teach",
                response_model=TeachLessonListResponse,
            )
            fetch_id = store.record_fetch(source="catalog", method="GET", url="test")
            store.store_response(
                table_name="catalog_teach_lesson_list_for_teach",
                response=response,
                fetch_id=fetch_id,
            )

            mapping = _stored_course_ids_by_code(store)
            stored_ids = store.conn.execute(
                """
                SELECT lesson.id, course.id
                FROM catalog_teach_lesson_list_for_teach AS lesson
                JOIN catalog_teach_lesson_list_for_teach_course AS course
                  ON course.parent_store_id = lesson.store_id
                """
            ).fetchone()
        finally:
            store.close()

        self.assertEqual(mapping, {"MATH1001": 144481})
        self.assertEqual(stored_ids, (181384, 144481))

    def test_rejects_conflicting_course_id_and_code_mappings(self) -> None:
        conflicting_code = TeachLessonListResponse(
            root=[
                _catalog_lesson(1, course_id=10, course_code="A"),
                _catalog_lesson(2, course_id=11, course_code="A"),
            ]
        )
        conflicting_id = TeachLessonListResponse(
            root=[
                _catalog_lesson(1, course_id=10, course_code="A"),
                _catalog_lesson(2, course_id=10, course_code="B"),
            ]
        )

        with self.assertRaisesRegex(ValueError, "maps to both"):
            _course_ids_by_code_from_response(conflicting_code)
        with self.assertRaisesRegex(ValueError, "maps to both"):
            _course_ids_by_code_from_response(conflicting_id)

    def test_rejects_changed_course_id_from_previous_snapshot(self) -> None:
        response = TeachLessonListResponse(
            root=[_catalog_lesson(1, course_id=11, course_code="A")]
        )

        with self.assertRaisesRegex(ValueError, "changed id from 10 to 11"):
            _course_ids_by_code_from_response(
                response, previous_course_ids_by_code={"A": 10}
            )

    def test_rejects_duplicate_lesson_and_missing_course(self) -> None:
        duplicate_lesson = TeachLessonListResponse(
            root=[
                _catalog_lesson(1, course_id=10, course_code="A"),
                _catalog_lesson(1, course_id=10, course_code="A"),
            ]
        )
        missing_course = TeachLessonListResponse(
            root=[_catalog_lesson(1, course_id=None, course_code=None)]
        )

        with self.assertRaisesRegex(ValueError, "Duplicate catalog lesson"):
            _course_ids_by_code_from_response(duplicate_lesson)
        with self.assertRaisesRegex(ValueError, "has no valid course"):
            _course_ids_by_code_from_response(missing_course)

    def test_rejects_course_without_chinese_name(self) -> None:
        response = TeachLessonListResponse(
            root=[
                _catalog_lesson(
                    1,
                    course_id=10,
                    course_code="A",
                    course_cn=None,
                )
            ]
        )

        with self.assertRaisesRegex(ValueError, "has no valid course name"):
            _course_ids_by_code_from_response(response)

    def test_rejects_multiple_stored_courses_for_one_lesson(self) -> None:
        response = TeachLessonListResponse(
            root=[_catalog_lesson(1, course_id=10, course_code="A")]
        )
        store = SQLiteModelStore(":memory:")
        try:
            store.register_response_model(
                table_name="catalog_teach_lesson_list_for_teach",
                response_model=TeachLessonListResponse,
            )
            fetch_id = store.record_fetch(source="catalog", method="GET", url="test")
            store.store_response(
                table_name="catalog_teach_lesson_list_for_teach",
                response=response,
                fetch_id=fetch_id,
            )
            parent_store_id = store.conn.execute(
                "SELECT store_id FROM catalog_teach_lesson_list_for_teach"
            ).fetchone()[0]
            store.conn.execute(
                """
                INSERT INTO catalog_teach_lesson_list_for_teach_course(
                    fetch_id, parent_store_id, id, code, cn, en
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (fetch_id, parent_store_id, 10, "A", None, None),
            )

            with self.assertRaisesRegex(ValueError, "has 2 courses"):
                _stored_course_ids_by_code(store)
        finally:
            store.close()


def _jw_payload(ids: list[int]) -> dict:
    return {
        "result": {
            "lessonList": [
                dict.fromkeys(LessonListItem.model_fields) | {"id": i} for i in ids
            ],
            "scheduleList": [],
            "scheduleGroupList": [],
        }
    }


class JwScheduleChunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_records_expected_count_and_accepts_all_successful_chunks(
        self,
    ) -> None:
        store = SQLiteModelStore(":memory:")
        guesses = MagicMock()
        try:
            catalog_fetch_id = store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/401",
                context={"semester_id": "401"},
            )
            store.conn.execute(
                "CREATE TABLE catalog_teach_lesson_list_for_teach("
                "store_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "fetch_id INTEGER NOT NULL)"
            )
            store.conn.executemany(
                "INSERT INTO catalog_teach_lesson_list_for_teach(fetch_id) VALUES(?)",
                [(catalog_fetch_id,)] * 101,
            )
            with patch(
                "src.curriculum.fetch_jw_schedule_table_json",
                new_callable=AsyncMock,
                side_effect=[_jw_payload(list(range(100))), _jw_payload([100])],
            ):
                await _store_jw_schedule_chunks(
                    session=MagicMock(),
                    store=store,
                    guesses=guesses,
                    contracts=ObservedContractCollector(),
                    semester_id="401",
                    catalog_response=TeachLessonListResponse(root=[]),
                    courses=[SimpleNamespace(id=i) for i in range(101)],
                )

            metadata_key = _jw_schedule_expected_chunk_count_key("401")
            expected_count = store.conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (metadata_key,),
            ).fetchone()
            chunk_size = store.conn.execute(
                "SELECT value FROM metadata WHERE key = 'jw_schedule_chunk_size'"
            ).fetchone()
            complete = _has_cached_jw_schedule(store, "401")
            store.conn.execute("DELETE FROM metadata WHERE key = ?", (metadata_key,))
            legacy_complete = _has_cached_jw_schedule(store, "401")
        finally:
            store.close()

        self.assertEqual(expected_count, ("2",))
        self.assertEqual(chunk_size, ("100",))
        self.assertTrue(complete)
        self.assertTrue(legacy_complete)

    async def test_missing_chunk_is_not_complete(self) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            store.put_metadata({_jw_schedule_expected_chunk_count_key("401"): 2})
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "401", "chunk_index": 0},
            )

            complete = _has_cached_jw_schedule(store, "401")
        finally:
            store.close()

        self.assertFalse(complete)

    async def test_failed_chunk_aborts_refresh_and_is_not_complete(self) -> None:
        store = SQLiteModelStore(":memory:")
        guesses = MagicMock()
        try:
            with (
                patch(
                    "src.curriculum.fetch_jw_schedule_table_json",
                    new_callable=AsyncMock,
                    side_effect=[
                        _jw_payload(list(range(100))),
                        JSONDecodeError("non-json", "<html>", 0),
                    ],
                ),
                self.assertRaises(JSONDecodeError),
            ):
                await _store_jw_schedule_chunks(
                    session=MagicMock(),
                    store=store,
                    guesses=guesses,
                    contracts=ObservedContractCollector(),
                    semester_id="401",
                    catalog_response=TeachLessonListResponse(root=[]),
                    courses=[SimpleNamespace(id=i) for i in range(101)],
                )

            complete = _has_cached_jw_schedule(store, "401")
        finally:
            store.close()

        self.assertFalse(complete)


class HistoricalSemesterFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_pre_2021_spring_and_summer_including_empty_lists(self):
        for semester_id in ("53", "201", "202"):
            with self.subTest(semester_id=semester_id):
                store = SQLiteModelStore(":memory:")
                _register_upstream_tables(store)
                try:
                    with (
                        patch(
                            "src.curriculum.fetch_courses_json",
                            new_callable=AsyncMock,
                            return_value=[],
                        ) as lessons,
                        patch(
                            "src.curriculum.fetch_exams_json",
                            new_callable=AsyncMock,
                            return_value=[],
                        ) as exams,
                        patch(
                            "src.curriculum.fetch_jw_schedule_table_json",
                            new_callable=AsyncMock,
                        ) as jw,
                    ):
                        await _store_semester(
                            session=MagicMock(),
                            store=store,
                            guesses=MagicMock(),
                            contracts=ObservedContractCollector(),
                            semester_id=semester_id,
                            previous_course_ids_by_code={},
                        )
                        await _store_catalog_exams(
                            session=MagicMock(),
                            store=store,
                            contracts=ObservedContractCollector(),
                            semester_id=semester_id,
                        )
                    self.assertEqual(
                        lessons.await_args.kwargs["semester_id"], semester_id
                    )
                    self.assertEqual(
                        exams.await_args.kwargs["semester_id"], semester_id
                    )
                    jw.assert_not_awaited()
                    self.assertEqual(
                        _cached_fresh_lesson_semester_ids(
                            store,
                            [_semester(semester_id)],
                            now_timestamp=datetime.now(UTC).timestamp(),
                        ),
                        {semester_id},
                    )
                finally:
                    store.close()

    async def test_rejects_empty_or_invalid_semester_inventory(self):
        for payload in (
            None,
            [],
            [
                {
                    "id": None,
                    "nameZh": None,
                    "code": None,
                    "start": None,
                    "end": None,
                    "isLast": None,
                }
            ],
        ):
            store = SQLiteModelStore(":memory:")
            try:
                with (
                    patch(
                        "src.curriculum.fetch_semesters_json",
                        AsyncMock(return_value=payload),
                    ),
                    self.assertRaises(ValueError),
                ):
                    await _store_catalog_semesters(
                        MagicMock(), store, ObservedContractCollector()
                    )
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM upstream_fetches"
                    ).fetchone()[0],
                    0,
                )
            finally:
                store.close()

    async def test_failed_jw_chunk_removes_partial_semester_and_can_retry(self):
        store = SQLiteModelStore(":memory:")
        _register_upstream_tables(store)
        guesses = MagicMock()
        payload = [
            _catalog_lesson(i, course_id=10, course_code="A").model_dump()
            for i in range(1, 102)
        ]
        try:
            with (
                patch(
                    "src.curriculum.fetch_courses_json", AsyncMock(return_value=payload)
                ),
                patch(
                    "src.curriculum.fetch_jw_schedule_table_json",
                    AsyncMock(
                        side_effect=[
                            _jw_payload(list(range(1, 101))),
                            httpx.ConnectError("connection failed"),
                        ]
                    ),
                ),
            ):
                await _store_semester(
                    session=MagicMock(),
                    store=store,
                    guesses=guesses,
                    contracts=ObservedContractCollector(),
                    semester_id="201",
                    previous_course_ids_by_code={},
                )
            self.assertEqual(
                store.conn.execute(
                    "SELECT source,ok,context FROM upstream_fetches"
                ).fetchall(),
                [("jw_ws_schedule_table_datum", 0, "semester_id=201")],
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM catalog_teach_lesson_list_for_teach"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM jw_ws_schedule_table_datum_result_lessonList"
                ).fetchone()[0],
                0,
            )
            self.assertFalse(_has_cached_jw_schedule(store, "201"))
            with patch("src.curriculum.fetch_courses_json", AsyncMock(return_value=[])):
                await _store_semester(
                    session=MagicMock(),
                    store=store,
                    guesses=guesses,
                    contracts=ObservedContractCollector(),
                    semester_id="201",
                    previous_course_ids_by_code={},
                )
            self.assertEqual(
                _cached_fresh_lesson_semester_ids(
                    store,
                    [_semester("201")],
                    now_timestamp=datetime.now(UTC).timestamp(),
                ),
                {"201"},
            )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM upstream_fetches WHERE ok=0"
                ).fetchone()[0],
                0,
            )
        finally:
            store.close()

    async def test_transport_errors_are_declared_but_local_protocol_errors_abort(self):
        for error in (
            httpx.ConnectError("connection failed"),
            httpx.ReadError("reset"),
            httpx.RemoteProtocolError("peer closed"),
        ):
            store = SQLiteModelStore(":memory:")
            _register_upstream_tables(store)
            try:
                with patch(
                    "src.curriculum.fetch_courses_json", AsyncMock(side_effect=error)
                ):
                    await _store_semester(
                        session=MagicMock(),
                        store=store,
                        guesses=MagicMock(),
                        contracts=ObservedContractCollector(),
                        semester_id="201",
                        previous_course_ids_by_code={},
                    )
                self.assertEqual(
                    store.conn.execute(
                        "SELECT source,ok FROM upstream_fetches"
                    ).fetchall(),
                    [("catalog_teach_lesson_list_for_teach", 0)],
                )
            finally:
                store.close()
        store = SQLiteModelStore(":memory:")
        try:
            with (
                patch(
                    "src.curriculum.fetch_courses_json",
                    AsyncMock(side_effect=httpx.LocalProtocolError("invalid request")),
                ),
                self.assertRaises(httpx.LocalProtocolError),
            ):
                await _store_semester(
                    session=MagicMock(),
                    store=store,
                    guesses=MagicMock(),
                    contracts=ObservedContractCollector(),
                    semester_id="201",
                    previous_course_ids_by_code={},
                )
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM upstream_fetches").fetchone()[
                    0
                ],
                0,
            )
        finally:
            store.close()

    async def test_unavailable_exams_are_recorded_as_failed_not_empty(self):
        request = httpx.Request(
            "GET", "https://catalog.ustc.edu.cn/api/teach/exam/list/201"
        )
        for error in (
            httpx.ReadTimeout("timed out"),
            httpx.ConnectError("connection failed"),
            httpx.RemoteProtocolError("peer closed"),
            httpx.HTTPStatusError(
                "Bad Gateway",
                request=request,
                response=httpx.Response(502, request=request),
            ),
        ):
            store = SQLiteModelStore(":memory:")
            try:
                with patch(
                    "src.curriculum.fetch_exams_json", AsyncMock(side_effect=error)
                ):
                    await _store_catalog_exams(
                        session=MagicMock(),
                        store=store,
                        contracts=ObservedContractCollector(),
                        semester_id="201",
                    )
                self.assertEqual(
                    store.conn.execute("SELECT ok FROM upstream_fetches").fetchall(),
                    [(0,)],
                )
                self.assertEqual(
                    _cached_fresh_exam_semester_ids(
                        store,
                        [_semester("201")],
                        now_timestamp=datetime.now(UTC).timestamp(),
                    ),
                    set(),
                )
            finally:
                store.close()

    async def test_invalid_exam_response_still_aborts(self):
        store = SQLiteModelStore(":memory:")
        try:
            with (
                patch("src.curriculum.fetch_exams_json", AsyncMock(return_value=None)),
                self.assertRaises(ValueError),
            ):
                await _store_catalog_exams(
                    session=MagicMock(),
                    store=store,
                    contracts=ObservedContractCollector(),
                    semester_id="201",
                )
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM upstream_fetches").fetchone()[
                    0
                ],
                0,
            )
        finally:
            store.close()

    async def test_rejects_incomplete_or_wrong_jw_lesson_ids(self):
        for payload in (
            {"result": None},
            _jw_payload([]),
            _jw_payload([2]),
            _jw_payload([1, 1]),
        ):
            store = SQLiteModelStore(":memory:")
            try:
                with (
                    patch(
                        "src.curriculum.fetch_jw_schedule_table_json",
                        AsyncMock(return_value=payload),
                    ),
                    self.assertRaises(ValueError),
                ):
                    await _store_jw_schedule_chunks(
                        session=MagicMock(),
                        store=store,
                        guesses=MagicMock(),
                        contracts=ObservedContractCollector(),
                        semester_id="201",
                        catalog_response=TeachLessonListResponse(root=[]),
                        courses=[SimpleNamespace(id=1)],
                    )
                self.assertFalse(_has_cached_jw_schedule(store, "201"))
            finally:
                store.close()


class CurriculumRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_fatal_collection_cancels_requests_before_rollback(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def collect(semester):
            if semester.id == "1":
                await started.wait()
                raise ValueError("incompatible response")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(ExceptionGroup):
            await _collect_semesters(
                [_semester("1"), _semester("2")], collect, description="test"
            )
        self.assertTrue(cancelled.is_set())

    async def test_retries_failed_exams_without_refetching_fresh_lesson_cache(self):
        semesters = [
            dict(
                id=i,
                nameZh=f"semester {i}",
                code=str(i),
                start="2021-01-01",
                end="2021-07-01",
                isLast=False,
            )
            for i in (53, 201, 202)
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("src.curriculum.BUILD_DIR", root),
                patch("src.curriculum.BASE_DIR", root),
                patch("src.curriculum.USTCSession"),
                patch(
                    "src.curriculum.fetch_semesters_json",
                    AsyncMock(return_value=semesters),
                ),
                patch(
                    "src.curriculum.fetch_departments_json", AsyncMock(return_value=[])
                ),
                patch(
                    "src.curriculum.fetch_courses_json", AsyncMock(return_value=[])
                ) as lessons,
                patch(
                    "src.curriculum.fetch_exams_json",
                    AsyncMock(side_effect=[[], httpx.ReadTimeout("timed out"), []]),
                ) as exams,
                patch("src.curriculum.publish_contract_artifacts", return_value=[]),
            ):
                await make_curriculum()
                store = SQLiteModelStore(root / "life-ustc-static.sqlite", reset=False)
                try:
                    metadata = dict(
                        store.conn.execute("SELECT key, value FROM metadata")
                    )
                    self.assertEqual(metadata["selected_semester_ids"], "53,201,202")
                    self.assertEqual(
                        metadata["catalog_exam_unavailable_semester_ids"], "201"
                    )
                    self.assertEqual(
                        metadata["catalog_exam_successful_semester_ids"], "53,202"
                    )
                    self.assertEqual(
                        metadata["curriculum_fetch_status"],
                        "partial_sources_unavailable",
                    )
                finally:
                    store.close()
                lessons.reset_mock()
                exams.reset_mock(side_effect=True)
                exams.return_value = []
                await make_curriculum()
                lessons.assert_not_awaited()
                self.assertEqual(exams.await_count, 1)
                self.assertEqual(exams.await_args.kwargs["semester_id"], "201")
                store = SQLiteModelStore(root / "life-ustc-static.sqlite", reset=False)
                try:
                    metadata = dict(
                        store.conn.execute("SELECT key, value FROM metadata")
                    )
                    self.assertEqual(
                        metadata["catalog_exam_unavailable_semester_ids"], ""
                    )
                    self.assertEqual(
                        metadata["catalog_exam_refreshed_semester_ids"], "201"
                    )
                    self.assertEqual(metadata["refreshed_semester_ids"], "")
                    self.assertEqual(metadata["curriculum_fetch_status"], "complete")
                    self.assertEqual(
                        store.conn.execute(
                            "SELECT COUNT(*) FROM upstream_fetches WHERE ok=0"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    store.close()


class SemesterCacheTest(unittest.TestCase):
    def test_semester_has_ended_only_when_end_date_is_before_now(self) -> None:
        self.assertTrue(_semester_has_ended(_semester("221", end_date=100), 101))
        self.assertFalse(_semester_has_ended(_semester("441", end_date=100), 100))
        self.assertFalse(_semester_has_ended(_semester("latest", end_date=0), 100))

    def test_refreshes_unended_or_missing_cached_semesters(self) -> None:
        semesters = [
            _semester("221", end_date=100),
            _semester("241", end_date=100),
            _semester("441", end_date=300),
            _semester("461", end_date=400),
        ]

        refreshed = _refresh_curriculum_semesters(
            semesters,
            cached_semester_ids={"221", "441"},
            now_timestamp=200,
        )

        self.assertEqual([semester.id for semester in refreshed], ["241", "441", "461"])

    def test_contract_verification_refreshes_every_selected_semester(self) -> None:
        semesters = [
            _semester("221", end_date=100),
            _semester("461", end_date=400),
        ]

        refreshed = _curriculum_semesters_to_refresh(
            semesters,
            cached_semester_ids={"221", "461"},
            now_timestamp=500,
            verify_upstream_contract=True,
        )

        self.assertEqual(refreshed, semesters)

    def test_cached_fresh_lesson_semester_ids_require_lesson_jw_and_exam_when_needed(
        self,
    ) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            lesson_221 = store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/221",
                context={"semester_id": "221"},
            )
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "221", "chunk_index": 0},
            )
            store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/381",
                context={"semester_id": "381"},
            )
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "381", "chunk_index": 0},
            )
            store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/401",
                context={"semester_id": "401"},
            )
            store.record_fetch(
                source="catalog_teach_exam_list",
                method="GET",
                url="exam/401",
                context={"semester_id": "401"},
            )
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "401", "chunk_index": 0},
            )
            store.record_fetch(
                source="catalog_teach_lesson_list_for_teach",
                method="GET",
                url="lesson/421",
                context={"semester_id": "421"},
            )
            store.record_fetch(
                source="jw_ws_schedule_table_datum",
                method="POST",
                url="jw",
                context={"semester_id": "421", "chunk_index": 0},
                ok=False,
                error="non-json",
            )
            for semester_id in ("221", "381", "401", "421"):
                store.put_metadata(
                    {_jw_schedule_expected_chunk_count_key(semester_id): 1}
                )

            cached = _cached_fresh_lesson_semester_ids(
                store,
                [
                    _semester("221"),
                    _semester("381"),
                    _semester("401"),
                    _semester("421"),
                ],
                now_timestamp=datetime.now(UTC).timestamp(),
            )

            self.assertEqual(cached, {"221", "381", "401"})
            self.assertEqual(
                _cached_fresh_exam_semester_ids(
                    store,
                    [_semester("221"), _semester("381"), _semester("401")],
                    now_timestamp=datetime.now(UTC).timestamp(),
                ),
                {"401"},
            )
            self.assertIsInstance(lesson_221, int)
        finally:
            store.close()

    def test_stale_exam_refreshes_independently_of_fresh_catalog_and_schedule(self):
        now = datetime.now(UTC).timestamp()
        store = SQLiteModelStore(":memory:")
        try:
            for source in (
                "catalog_teach_lesson_list_for_teach",
                "catalog_teach_exam_list",
                "jw_ws_schedule_table_datum",
            ):
                context = {"semester_id": "201"}
                if source == "jw_ws_schedule_table_datum":
                    context["chunk_index"] = 0
                store.record_fetch(
                    source=source, method="GET", url="test", context=context
                )
            store.put_metadata({_jw_schedule_expected_chunk_count_key("201"): 1})
            store.conn.execute(
                "UPDATE upstream_fetches SET fetched_at=?",
                (datetime.fromtimestamp(now - 1, UTC).isoformat(),),
            )
            semesters = [_semester("201", end_date=1)]
            self.assertEqual(
                _cached_fresh_exam_semester_ids(store, semesters, now_timestamp=now),
                {"201"},
            )
            store.conn.execute(
                "UPDATE upstream_fetches SET fetched_at=? "
                "WHERE source='catalog_teach_exam_list'",
                (
                    datetime.fromtimestamp(
                        now - ENDED_SEMESTER_CACHE_MAX_AGE_SECONDS, UTC
                    ).isoformat(),
                ),
            )
            cached_lessons = _cached_fresh_lesson_semester_ids(
                store, semesters, now_timestamp=now
            )
            cached_exams = _cached_fresh_exam_semester_ids(
                store, semesters, now_timestamp=now
            )
            self.assertEqual(cached_lessons, {"201"})
            self.assertEqual(cached_exams, set())
            self.assertEqual(
                _refresh_curriculum_semesters(
                    semesters, cached_semester_ids=cached_lessons, now_timestamp=now
                ),
                [],
            )
            self.assertEqual(
                _refresh_curriculum_semesters(
                    semesters, cached_semester_ids=cached_exams, now_timestamp=now
                ),
                semesters,
            )
            store.conn.execute(
                "UPDATE upstream_fetches SET fetched_at=? "
                "WHERE source='jw_ws_schedule_table_datum'",
                (
                    datetime.fromtimestamp(
                        now - ENDED_SEMESTER_CACHE_MAX_AGE_SECONDS, UTC
                    ).isoformat(),
                ),
            )
            self.assertEqual(
                _cached_fresh_lesson_semester_ids(store, semesters, now_timestamp=now),
                set(),
            )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
