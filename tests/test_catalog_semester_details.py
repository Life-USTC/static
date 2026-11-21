import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src.models.api.catalog_api_teach_lesson_infos import TeachLessonInfo
from src.models.api.catalog_api_teach_lesson_list_for_teach import (
    TeachLessonListItem,
    TeachLessonListResponse,
)
from src.platforms.catalog.sections import (
    SEMESTER_LIST_URL,
    refresh_catalog_semester_details,
)
from src.sqlite_store import SQLiteModelStore
from src.upstream_contracts import CATALOG_LESSON_INFOS as PROFILES
from src.upstream_contracts import CATALOG_LESSONS as CURRICULUM

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
ACTIVE, OLD = 461, 221
KEY = f"catalog_semester_details_{OLD}"
CONTEXT = {"semester_id": str(OLD)}


def term(id_, start, end):
    return dict(
        id=id_, start=start, end=end, code=str(id_), nameZh="学期", isLast=id_ == ACTIVE
    )


def lesson(id_, code):
    return dict.fromkeys(TeachLessonListItem.model_fields) | dict(
        id=id_, code=code, course=dict(id=id_ + 100, code=code, cn="课", en=None)
    )


def profile(row):
    fields = TeachLessonInfo.model_fields
    return dict.fromkeys(field.alias or name for name, field in fields.items()) | dict(
        id=row["id"],
        code=row["code"],
        courseId=row["course"]["id"],
        valid=True,
        name={"cn": "课", "en": None},
        textbooks=[],
    )


class SectionRefreshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SQLiteModelStore(":memory:")
        self.addCleanup(self.store.close)
        self.db = self.store.conn
        self.clock = self.enterContext(
            patch("src.platforms.catalog.sections.datetime", wraps=datetime)
        )
        self.clock.now.return_value = NOW
        self.enterContext(
            patch.multiple(
                "src.platforms.catalog.sections",
                REQUEST_INTERVAL_SECONDS=0,
                BATCH_SIZE=2,
                MISSING_PROBE_LIMIT=1,
            )
        )
        self.terms = [
            term(ACTIVE, "2026-08-01", "2027-01-15"),
            term(OLD, "2021-09-01", "2022-01-21"),
        ]
        self.index = {i: [lesson(i, "A"), lesson(i + 1, "B")] for i in (ACTIVE, OLD)}
        self.gets, self.omit, self.changed = [], set(), {}
        self.index_status = self.profile_status = 200
        self.infos_only = []

    async def get(self, url, **_):
        if url == SEMESTER_LIST_URL:
            return self.terms
        id_ = int(url.rsplit("/", 1)[-1])
        self.gets.append(id_)
        if id_ == OLD and self.index_status != 200:
            httpx.Response(
                self.index_status, request=httpx.Request("GET", url)
            ).raise_for_status()
        return self.index[id_]

    async def post(self, url, data, **_):
        id_, codes = data["semester"], data["codes"]
        if id_ == OLD and self.profile_status != 200:
            httpx.Response(
                self.profile_status, request=httpx.Request("POST", url)
            ).raise_for_status()
        return [
            profile(row) | self.changed.get(row["code"], {})
            for row in self.index[id_] + self.infos_only
            if row["code"] in codes and row["code"] not in self.omit
        ]

    async def refresh(self, *terms):
        self.gets.clear()
        api = SimpleNamespace(get_json=self.get, post_json=self.post)
        await refresh_catalog_semester_details(
            api, self.store, semester_ids=terms or None
        )

    def seed_curriculum(self, *, ok=True):
        with self.db:
            rows = self.db.execute(
                "SELECT id FROM upstream_fetches WHERE source=?", (CURRICULUM,)
            )
            self.store.delete_fetches([id_ for (id_,) in rows])
            url = f"https://catalog.ustc.edu.cn/api/teach/lesson/list-for-teach/{OLD}"
            fetch = self.store.record_fetch(
                source=CURRICULUM, method="GET", url=url, context=CONTEXT, ok=ok
            )
            response = TeachLessonListResponse.model_validate(self.index[OLD])
            self.store.store_response(
                table_name=CURRICULUM,
                response=response,
                fetch_id=fetch,
                context=CONTEXT,
            )
            self.store.put_metadata(
                dict(
                    curriculum_fetch_status="complete",
                    curriculum_successful_semester_ids=str(OLD),
                )
            )

    def saved(self, id_):
        rows = self.db.execute(
            f"SELECT code,payload_json FROM {PROFILES} WHERE semester_id=?", (str(id_),)
        )
        return {code: json.loads(payload) for code, payload in rows}

    def old_meta(self):
        value = self.db.execute(
            "SELECT value FROM metadata WHERE key=?", (KEY,)
        ).fetchone()[0]
        return json.loads(value)

    async def test_settled_archive_profile_and_live_expiry(self):
        self.seed_curriculum()
        await self.refresh()
        self.assertEqual(self.gets, [ACTIVE])
        self.assertEqual(self.old_meta()["lesson_index_source"], "curriculum")
        self.assertEqual(
            self.saved(OLD), {row["code"]: profile(row) for row in self.index[OLD]}
        )
        self.changed["B"] = {"desc": {"cn": "更新教学大纲", "en": None}}
        self.clock.now.return_value += timedelta(days=29)
        await self.refresh()
        self.assertNotIn(OLD, self.gets)
        self.clock.now.return_value += timedelta(days=1)
        await self.refresh()
        self.assertIn(OLD, self.gets)
        self.assertEqual(self.saved(OLD)["B"]["desc"], self.changed["B"]["desc"])

    async def test_changed_curriculum_cannot_silently_override_live_history(self):
        await self.refresh(OLD)
        original = self.saved(OLD)
        added = lesson(19, "C")
        self.index[OLD].append(added)
        self.seed_curriculum()
        self.index[OLD].pop()
        await self.refresh()
        self.assertIn(OLD, self.gets)
        self.assertEqual(self.saved(OLD), original)
        await self.refresh()
        self.assertNotIn(OLD, self.gets)
        self.index[OLD].append(lesson(20, "C"))
        self.seed_curriculum()
        await self.refresh()
        self.assertEqual(set(self.saved(OLD)), {"A", "B", "C"})

    async def test_failed_or_omitted_archive_identity_falls_back_to_live(self):
        self.seed_curriculum(ok=False)
        await self.refresh()
        self.assertIn(OLD, self.gets)
        rows = self.db.execute(
            "SELECT id FROM upstream_fetches WHERE context=? AND source!=?",
            (f"semester_id={OLD}", CURRICULUM),
        )
        self.store.delete_fetches([id_ for (id_,) in rows])
        self.db.execute("DELETE FROM metadata WHERE key=?", (KEY,))
        self.seed_curriculum()
        self.index[OLD][-1] = lesson(7, "C")
        await self.refresh()
        self.assertEqual(set(self.saved(OLD)), {"A", "C"})
        self.assertEqual(self.old_meta()["lesson_index_source"], "public")

    async def test_explicit_missing_history_leaves_active_rows_usable(self):
        self.index_status = 410
        with self.assertRaisesRegex(RuntimeError, str(OLD)):
            await self.refresh(OLD)
        self.seed_curriculum()
        self.profile_status = 404
        await self.refresh()
        self.assertTrue(self.saved(ACTIVE))
        self.assertTrue(self.old_meta()["lesson_infos_unavailable"])
        self.assertEqual(self.old_meta()["lesson_index_source"], "cached")

    async def test_suspicious_shrink_and_wrong_identity_keep_last_verified_snapshot(
        self,
    ):
        await self.refresh()
        expected = self.saved(ACTIVE)
        self.infos_only = [self.index[ACTIVE].pop()]
        with self.assertRaisesRegex(RuntimeError, str(ACTIVE)):
            await self.refresh()
        self.assertEqual(self.saved(ACTIVE), expected)
        self.infos_only = []
        self.changed["A"] = {"id": 999}
        with self.assertRaisesRegex(RuntimeError, str(ACTIVE)):
            await self.refresh()
        self.assertEqual(self.saved(ACTIVE), expected)
        self.changed.clear()
        await self.refresh()
        self.assertEqual(set(self.saved(ACTIVE)), {"A"})
        self.index[ACTIVE].append(lesson(82, "MISSING"))
        self.omit.add("MISSING")
        await self.refresh()
        self.assertEqual(set(self.saved(ACTIVE)), {"A"})
