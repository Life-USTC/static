from __future__ import annotations

import asyncio
import json
import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from src.sqlite_store import SQLiteModelStore
from src.study_plans import (
    COURSE_MODULE_TABLE,
    COURSE_MODULE_URL,
    FETCH_CONCURRENCY,
    PROGRAM_COURSES_TABLE,
    PROGRAM_INFO_TABLE,
    PROGRAM_INFO_URL,
    PROGRAM_TREE_TABLE,
    PROGRAM_TREE_URL,
    REFRESH_AGE,
    refresh_study_plans,
)


def membership(code: str, *, compulsory: bool, terms: list[str]) -> dict:
    return {
        "course": {"code": code, "id": 15584, "introduction": "<p>课程</p>"},
        "compulsory": compulsory,
        "terms": terms,
    }


def node(id_: int, *, public: int | None = None, children=(), courses=()) -> dict:
    return {
        "self": {
            "id": id_,
            "type": f"Module {id_}",
            "typeEn": f"Module {id_}",
            "public": public,
            "ref": 5000,
            "courses": list(courses),
        },
        "isLeaf": not children,
        "children": list(children),
    }


def plan(*nodes: dict) -> dict:
    return {"moduleTree": list(nodes), "beginSemester": "2025年秋季学期"}


def tree(*plans: int) -> dict:
    return {
        "91": {
            "id": 91,
            "nameZh": "地球和空间科学学院",
            "majors": {
                "29": {
                    "id": 29,
                    "nameZh": "大气科学",
                    "programs": [
                        {
                            "id": id_,
                            "grade": "2025",
                            "trainType": "主修",
                            "nameZh": f"Plan {id_}",
                        }
                        for id_ in plans
                    ],
                }
            },
        }
    }


class Session:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: Counter[str] = Counter()
        self.in_progress = 0
        self.max_in_progress = 0

    async def get_json(self, url: str, **_kwargs):
        self.calls[url] += 1
        self.in_progress += 1
        self.max_in_progress = max(self.max_in_progress, self.in_progress)
        try:
            await asyncio.sleep(0)
            result = self.routes[url]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self.in_progress -= 1


class StudyPlansTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SQLiteModelStore(":memory:")
        self.addCleanup(self.store.close)
        self.enterContext(patch("src.study_plans.FETCH_INTERVAL_SECONDS", 0))

    async def refresh(self, routes: dict) -> Session:
        session = Session(routes)
        await refresh_study_plans(session, self.store)
        return session

    def read(self, sql: str):
        return self.store.conn.execute(sql).fetchall()

    async def test_cold_reference_graph_preserves_distinct_membership_context(self):
        routes = {
            PROGRAM_TREE_URL: tree(*range(1, 10)),
            f"{PROGRAM_INFO_URL}/1": plan(
                node(
                    10,
                    public=100,
                    children=[
                        node(
                            11,
                            courses=[membership("001006", compulsory=True, terms=[])],
                        ),
                        node(12, public=100),
                    ],
                )
            ),
            f"{PROGRAM_INFO_URL}/2": plan(node(20, public=100)),
            f"{COURSE_MODULE_URL}/100": node(
                100,
                public=100,
                children=[
                    node(
                        101,
                        public=200,
                        courses=[
                            membership("ATMS4003", compulsory=False, terms=["3春"])
                        ],
                    )
                ],
            ),
            f"{COURSE_MODULE_URL}/200": node(
                200,
                public=100,
                courses=[membership("001357", compulsory=True, terms=["3秋"])],
            ),
        }
        for id_ in range(3, 10):
            routes[f"{PROGRAM_INFO_URL}/{id_}"] = plan()
        session = await self.refresh(routes)

        self.assertGreater(session.max_in_progress, 1)
        self.assertLessEqual(session.max_in_progress, FETCH_CONCURRENCY)
        self.assertEqual(session.calls, Counter(routes.keys()))
        raw_tree = self.read(f"SELECT payload_json FROM {PROGRAM_TREE_TABLE}")[0][0]
        self.assertEqual(json.loads(raw_tree), routes[PROGRAM_TREE_URL])
        raw_plan = self.read(
            f"SELECT payload_json FROM {PROGRAM_INFO_TABLE} WHERE program_id=1"
        )[0][0]
        self.assertEqual(json.loads(raw_plan), routes[f"{PROGRAM_INFO_URL}/1"])
        raw_module = self.read(
            f"SELECT payload_json FROM {COURSE_MODULE_TABLE} WHERE module_id=200"
        )[0][0]
        self.assertEqual(json.loads(raw_module), routes[f"{COURSE_MODULE_URL}/200"])
        rows = self.read(
            f"""
            SELECT program_id, course_code, module_id, public_module_id,
                   module_path_json, compulsory, terms_json
            FROM {PROGRAM_COURSES_TABLE} ORDER BY program_id, position
            """
        )
        self.assertEqual(
            rows,
            [
                (1, "001006", 11, None, "[10,11]", 1, "[]"),
                (1, "ATMS4003", 101, 100, "[10,12,100,101]", 0, '["3春"]'),
                (1, "001357", 200, 200, "[10,12,100,101,200]", 1, '["3秋"]'),
                (1, "ATMS4003", 101, 100, "[10,100,101]", 0, '["3春"]'),
                (1, "001357", 200, 200, "[10,100,101,200]", 1, '["3秋"]'),
                (2, "ATMS4003", 101, 100, "[20,100,101]", 0, '["3春"]'),
                (2, "001357", 200, 200, "[20,100,101,200]", 1, '["3秋"]'),
            ],
        )

    async def test_incremental_refresh_reuses_fresh_records_and_ages_modules(self):
        routes = {
            PROGRAM_TREE_URL: tree(1, 2),
            f"{PROGRAM_INFO_URL}/1": plan(node(10, public=100)),
            f"{PROGRAM_INFO_URL}/2": plan(node(20)),
            f"{COURSE_MODULE_URL}/100": node(100),
        }
        await self.refresh(routes)
        registry = self.read(
            f"SELECT program_id, fetch_id FROM {PROGRAM_INFO_TABLE} ORDER BY program_id"
        )
        session = await self.refresh({PROGRAM_TREE_URL: tree(1, 2)})
        self.assertEqual(session.calls, {PROGRAM_TREE_URL: 1})
        self.assertEqual(
            self.read(
                f"SELECT program_id, fetch_id FROM {PROGRAM_INFO_TABLE} "
                "ORDER BY program_id"
            ),
            registry,
        )

        expired_at = datetime.now(UTC) - REFRESH_AGE - timedelta(days=1)
        self.store.conn.execute(
            "UPDATE upstream_fetches SET fetched_at=? WHERE source=?",
            (expired_at.isoformat(), COURSE_MODULE_TABLE),
        )
        latest_index = tree(1)
        latest_index["91"]["majors"]["29"]["programs"][0]["nameZh"] = "Updated"
        latest = {
            PROGRAM_TREE_URL: latest_index,
            f"{PROGRAM_INFO_URL}/1": routes[f"{PROGRAM_INFO_URL}/1"],
            f"{COURSE_MODULE_URL}/100": node(
                100, courses=[membership("MATH1006", compulsory=False, terms=["1秋"])]
            ),
        }
        session = await self.refresh(latest)
        self.assertEqual(
            session.calls,
            {
                PROGRAM_TREE_URL: 2,
                f"{PROGRAM_INFO_URL}/1": 1,
                f"{COURSE_MODULE_URL}/100": 1,
            },
        )
        self.assertEqual(
            self.read(f"SELECT program_id FROM {PROGRAM_INFO_TABLE}"), [(1,)]
        )
        self.assertEqual(
            self.read(f"SELECT program_id, course_code FROM {PROGRAM_COURSES_TABLE}"),
            [(1, "MATH1006")],
        )

    async def test_fetch_or_projection_error_preserves_previous_snapshot(self):
        url = f"{PROGRAM_INFO_URL}/1"
        keys = (
            PROGRAM_TREE_TABLE,
            PROGRAM_INFO_TABLE,
            PROGRAM_COURSES_TABLE,
            "upstream_fetches",
        )
        original_index = tree(1)
        await self.refresh(
            {
                PROGRAM_TREE_URL: original_index,
                url: plan(
                    node(10, courses=[membership("00001", compulsory=True, terms=[])])
                ),
            }
        )
        before = {key: self.read(f"SELECT * FROM {key}") for key in keys}
        changed_index = tree(1)
        changed_index["91"]["majors"]["29"]["programs"][0]["nameZh"] = "New"

        with self.assertRaises(ExceptionGroup):
            await self.refresh(
                {PROGRAM_TREE_URL: changed_index, url: RuntimeError("offline")}
            )
        self.assertEqual(
            {key: self.read(f"SELECT * FROM {key}") for key in keys}, before
        )

        broken = {"course": {"id": 1, "code": True}}
        with self.assertRaisesRegex(ValueError, "course.code must be text"):
            await self.refresh(
                {PROGRAM_TREE_URL: changed_index, url: plan(node(10, courses=[broken]))}
            )
        self.assertEqual(
            {key: self.read(f"SELECT * FROM {key}") for key in keys}, before
        )
