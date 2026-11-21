import json
import sqlite3
import unittest
from unittest.mock import AsyncMock

from src.models.api.catalog_api_teach_course_substitutes import (
    CATALOG_COURSE_SUBSTITUTES as TABLE,
)
from src.platforms.catalog.substitutions import refresh_course_substitutions
from src.sqlite_store import SQLiteModelStore


class CourseSubstitutionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_order_replacement_and_rollback(self):
        store = SQLiteModelStore(":memory:")
        self.addCleanup(store.close)
        course = {
            "id": 1,
            "cn": "课程",
            "en": "Course",
            "code": "001",
            "period": 40,
            "credits": 2,
        }
        rule = {
            "id": 8,
            "originalCourses": [course, {**course, "id": 2, "code": "002"}],
            "substituteCourses": [{**course, "id": 3, "code": "003"}],
        }

        async def refresh(rules):
            session = AsyncMock()
            session.get_json.return_value = rules
            await refresh_course_substitutions(session, store)

        await refresh([rule, {**rule, "id": 9}])
        await refresh([rule])
        rows = store.conn.execute(f"SELECT payload_json FROM {TABLE}").fetchall()
        self.assertEqual([json.loads(payload) for (payload,) in rows], [rule])
        codes = store.conn.execute(
            f"SELECT code FROM {TABLE}_originalCourses ORDER BY position"
        )
        self.assertEqual(codes.fetchall(), [("001",), ("002",)])
        store.conn.execute(
            f"CREATE TRIGGER reject BEFORE INSERT ON {TABLE}_substituteCourses "
            "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
        )
        previous = list(store.conn.iterdump())
        for rules, error in (
            ([], ValueError),
            ([rule, rule], ValueError),
            ([rule], sqlite3.IntegrityError),
        ):
            with self.assertRaises(error):
                await refresh(rules)
            self.assertEqual(list(store.conn.iterdump()), previous)
