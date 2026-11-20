import json
import sqlite3
import unittest

import httpx
from pydantic import ValidationError

from src.course_substitutions import (
    COURSE_SUBSTITUTES_URL,
    refresh_course_substitutions,
)
from src.models.api.catalog_api_teach_course_substitutes import (
    CATALOG_COURSE_SUBSTITUTES,
)
from src.sqlite_store import SQLiteModelStore
from src.utils.auth import RequestSession

ORIGINALS = f"{CATALOG_COURSE_SUBSTITUTES}_originalCourses"
SUBSTITUTES = f"{CATALOG_COURSE_SUBSTITUTES}_substituteCourses"
METADATA_COUNT = f"{CATALOG_COURSE_SUBSTITUTES}_count"


def rule(rule_id: int) -> dict:
    return {
        "id": rule_id,
        "originalCourses": [
            {
                "id": 1,
                "cn": "原课一",
                "en": "First original course",
                "code": "001001",
                "period": 30,
                "credits": 1.5,
            },
            {
                "id": 2,
                "cn": "原课二",
                "en": "Second original course",
                "code": "001002",
                "period": 40,
                "credits": 2,
            },
        ],
        "substituteCourses": [
            {
                "id": 3,
                "cn": "替代课程",
                "en": "Substitute course",
                "code": "001003",
                "period": 60,
                "credits": 3,
            }
        ],
    }


class CourseSubstituteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = SQLiteModelStore(":memory:")
        self.addCleanup(self.store.close)
        self.payload: object = [rule(10), rule(11)]
        self.status = 200
        self.calls = 0
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(client.aclose)
        self.session = RequestSession(client=client, page=None)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.assertEqual(
            (request.method, str(request.url)), ("GET", COURSE_SUBSTITUTES_URL)
        )
        return httpx.Response(self.status, json=self.payload)

    def stored_rules(self) -> list[tuple[int, int, dict]]:
        return [
            (rule_id, fetch_id, json.loads(payload))
            for rule_id, fetch_id, payload in self.store.conn.execute(
                f"SELECT id, fetch_id, payload_json FROM {CATALOG_COURSE_SUBSTITUTES} "
                "ORDER BY position"
            )
        ]

    def snapshot(self) -> tuple:
        return (
            self.stored_rules(),
            self.store.conn.execute(f"SELECT * FROM {ORIGINALS}").fetchall(),
            self.store.conn.execute(f"SELECT * FROM {SUBSTITUTES}").fetchall(),
            self.store.conn.execute("SELECT * FROM upstream_fetches").fetchall(),
            self.store.conn.execute(
                "SELECT value FROM metadata WHERE key = ?", (METADATA_COUNT,)
            ).fetchone(),
        )

    async def test_stores_direction_order_names_and_exact_payload(self) -> None:
        await refresh_course_substitutions(self.session, self.store)
        self.assertEqual(self.calls, 1)
        self.assertEqual(
            [payload for _, _, payload in self.stored_rules()], self.payload
        )
        self.assertEqual(
            self.store.conn.execute(
                f"SELECT code, cn, en, period, credits, position FROM {ORIGINALS} "
                "ORDER BY parent_store_id, position"
            ).fetchall()[:2],
            [
                ("001001", "原课一", "First original course", 30, 1.5, 0),
                ("001002", "原课二", "Second original course", 40, 2, 1),
            ],
        )
        self.assertEqual(
            self.store.conn.execute(f"SELECT code FROM {SUBSTITUTES}").fetchall(),
            [("001003",), ("001003",)],
        )
        self.assertEqual(self.snapshot()[-1], ("2",))

        old_fetch = self.stored_rules()[0][1]
        self.payload = [rule(17)]
        await refresh_course_substitutions(self.session, self.store)
        self.assertEqual(self.calls, 2)
        self.assertEqual([row[0] for row in self.stored_rules()], [17])
        self.assertNotEqual(self.stored_rules()[0][1], old_fetch)
        self.assertEqual(self.snapshot()[-1], ("1",))
        self.assertEqual(len(self.snapshot()[1]), 2)
        self.assertEqual(len(self.snapshot()[2]), 1)
        self.assertEqual(len(self.snapshot()[3]), 1)

    async def test_keeps_old_snapshot_on_bad_responses(self) -> None:
        await refresh_course_substitutions(self.session, self.store)
        previous = self.snapshot()
        values = [
            ({"unexpected": "shape"}, ValidationError),
            ([], ValidationError),
            ([{**rule(2), "originalCourses": []}], ValidationError),
            ([rule(3), rule(3)], ValueError),
        ]
        for self.payload, error in values:
            with self.assertRaises(error):
                await refresh_course_substitutions(self.session, self.store)
            self.assertEqual(self.snapshot(), previous)
        self.status = 503
        with self.assertRaises(httpx.HTTPStatusError):
            await refresh_course_substitutions(self.session, self.store)
        self.assertEqual(self.snapshot(), previous)
        self.assertEqual(self.calls, 6)

    async def test_storage_failure_rolls_back_both_sides_and_fetch(self) -> None:
        await refresh_course_substitutions(self.session, self.store)
        previous = self.snapshot()
        self.store.conn.execute(
            f"CREATE TRIGGER reject_substitute BEFORE INSERT ON {SUBSTITUTES} "
            "BEGIN SELECT RAISE(ABORT, 'test write failure'); END"
        )
        self.payload = [rule(20)]
        with self.assertRaises(sqlite3.IntegrityError):
            await refresh_course_substitutions(self.session, self.store)
        self.assertEqual(self.snapshot(), previous)
