import json
import unittest
from unittest.mock import AsyncMock, patch

from src.platforms.jw import (
    COURSES,
    LESSONS,
    fetch_jw_courses_json,
    refresh_jw_profiles,
)
from src.sqlite_store import SQLiteModelStore


class JwProfileSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_paginated_list_checks_completeness(self):
        def page(index, total=201):
            rows = [
                {"id": i} for i in range((index - 1) * 200, min(index * 200, total))
            ]
            return {
                "data": rows,
                "_page_": {
                    "currentPage": index,
                    "rowsInPage": len(rows),
                    "rowsPerPage": 200,
                    "totalRows": total,
                    "totalPages": 2,
                },
            }

        session = AsyncMock()
        session.get_json.side_effect = [page(1), page(2)]
        with patch("src.platforms.jw._get_jw_user_id", AsyncMock(return_value="1")):
            self.assertEqual(
                len((await fetch_jw_courses_json(session, "461"))["data"]), 201
            )
            session.get_json.side_effect = [page(1), page(2, 202)]
            with self.assertRaisesRegex(ValueError, "changed during pagination"):
                await fetch_jw_courses_json(session, "461")

    async def test_profile_refresh_retains_failed_semester_and_skips_fresh_data(self):
        store = SQLiteModelStore(":memory:")
        self.addCleanup(store.close)
        course = {
            "id": 1,
            "code": "001",
            "nameZh": "测试",
            "credits": 0,
            "introduction": "简介",
            "enabled": True,
        }
        row = {
            "id": 2,
            "code": "001.01",
            "course": course,
            "semester": {"id": 461, "endDate": "2027-01-15"},
            "credits": 2,
            "teacherAssignmentList": [{"person": {"private": "discard"}}],
        }
        with (
            patch(
                "src.platforms.jw.fetch_jw_semesters",
                AsyncMock(return_value=[("461", "2026年秋季学期")]),
            ) as terms,
            patch(
                "src.platforms.jw.fetch_jw_courses_json",
                AsyncMock(return_value={"data": [row]}),
            ) as fetch,
        ):
            await refresh_jw_profiles(AsyncMock(), store)
            await refresh_jw_profiles(AsyncMock(), store)
            fetch.assert_awaited_once()
            self.assertEqual(terms.await_count, 2)
            profile = json.loads(
                store.conn.execute(f"SELECT payload_json FROM {COURSES}").fetchone()[0]
            )
            self.assertEqual(profile["credit"], 0)
            self.assertEqual(profile["desc"]["cn"], "简介")
            self.assertNotIn("private", json.dumps(profile))
            self.assertEqual(
                store.conn.execute(f"SELECT courseId FROM {LESSONS}").fetchone()[0], 1
            )
            store.conn.execute(
                "DELETE FROM metadata WHERE key='jw_catalog_semester_461'"
            )
            store.conn.commit()
            fetch.side_effect = RuntimeError("request failed")
            with self.assertRaisesRegex(RuntimeError, "request failed"):
                await refresh_jw_profiles(AsyncMock(), store)
            self.assertEqual(
                store.conn.execute(f"SELECT COUNT(*) FROM {LESSONS}").fetchone()[0], 1
            )
            self.assertEqual(
                store.conn.execute("PRAGMA foreign_key_check").fetchall(), []
            )
