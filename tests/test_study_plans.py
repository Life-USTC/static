import json
import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta

from src.sqlite_store import SQLiteModelStore
from src.study_plans import (
    COURSE_MODULE_TABLE,
    COURSE_MODULE_URL,
    PROGRAM_COURSES_TABLE,
    PROGRAM_INFO_TABLE,
    PROGRAM_INFO_URL,
    PROGRAM_TREE_URL,
    REFRESH_AGE,
    refresh_study_plans,
)


def node(id_, *, public=None, children=(), code=None):
    course = {"course": {"code": code, "id": 7}, "compulsory": False, "terms": ["3春"]}
    module = {"id": id_, "public": public, "courses": [] if code is None else [course]}
    return {"self": module, "isLeaf": not children, "children": list(children)}


def tree(*ids, grade="2025"):
    plans = [{"id": id_, "grade": grade, "trainType": "主修"} for id_ in ids]
    major = {"id": 29, "programs": plans}
    return {"91": {"id": 91, "majors": {"29": major}}}


class StudyPlansTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SQLiteModelStore(":memory:")
        self.addCleanup(self.store.close)
        self.calls = Counter()
        self.routes = {
            PROGRAM_TREE_URL: tree(1, 2),
            f"{PROGRAM_INFO_URL}/1": {
                "moduleTree": [node(10, public=100, children=[node(11, public=100)])]
            },
            f"{PROGRAM_INFO_URL}/2": {"moduleTree": [node(20)]},
            f"{COURSE_MODULE_URL}/100": node(100, public=200, code="ATMS4003"),
            f"{COURSE_MODULE_URL}/200": node(200, public=100, code="001357"),
        }

    async def get_json(self, url, **_kwargs):
        self.calls[url] += 1
        return self.routes[url]

    def read(self, sql):
        return self.store.conn.execute(sql).fetchall()

    async def test_reference_graph_and_fresh_reuse(self):
        await refresh_study_plans(self, self.store)
        self.assertEqual(self.calls, Counter(self.routes.keys()))
        self.assertEqual(
            self.read(
                "SELECT course_code, module_path_json, compulsory, terms_json "
                f"FROM {PROGRAM_COURSES_TABLE} ORDER BY position"
            ),
            [
                ("ATMS4003", "[10,11,100]", 0, '["3春"]'),
                ("001357", "[10,11,100,200]", 0, '["3春"]'),
                ("ATMS4003", "[10,100]", 0, '["3春"]'),
                ("001357", "[10,100,200]", 0, '["3春"]'),
            ],
        )
        table = PROGRAM_INFO_TABLE
        sql = f"SELECT fetch_id, payload_json FROM {table} WHERE program_id = 1"
        raw = self.read(sql)
        self.assertEqual(json.loads(raw[0][1]), self.routes[f"{PROGRAM_INFO_URL}/1"])
        self.calls.clear()
        await refresh_study_plans(self, self.store)
        self.assertEqual(self.calls, {PROGRAM_TREE_URL: 1})
        self.assertEqual(self.read(sql), raw)

    async def test_failed_projection_and_stale_refresh(self):
        await refresh_study_plans(self, self.store)
        expired = (datetime.now(UTC) - REFRESH_AGE - timedelta(days=1)).isoformat()
        self.store.conn.execute(
            "UPDATE upstream_fetches SET fetched_at=? WHERE source=?",
            (expired, COURSE_MODULE_TABLE),
        )
        dump = self.store.conn.iterdump
        before = list(dump())
        self.routes[PROGRAM_TREE_URL] = tree(1, grade="2026")
        self.routes[f"{COURSE_MODULE_URL}/100"] = node(100, code=True)
        with self.assertRaisesRegex(ValueError, "course.code must be text"):
            await refresh_study_plans(self, self.store)
        self.assertEqual(list(dump()), before)

        self.routes[f"{COURSE_MODULE_URL}/100"] = node(100, code="MATH1006")
        self.calls.clear()
        await refresh_study_plans(self, self.store)
        expected = {PROGRAM_TREE_URL: 2, f"{PROGRAM_INFO_URL}/1": 1}
        expected[f"{COURSE_MODULE_URL}/100"] = 1
        self.assertEqual(self.calls, expected)
        self.assertEqual(
            self.read(f"SELECT program_id FROM {PROGRAM_INFO_TABLE}"), [(1,)]
        )
        self.assertEqual(
            self.read(f"SELECT course_code FROM {PROGRAM_COURSES_TABLE}"),
            [("MATH1006",)] * 2,
        )
