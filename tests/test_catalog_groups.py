import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from src.catalog_groups import (
    CATALOG_GROUP_ENDPOINTS,
    CATALOG_GROUP_ENTRIES,
    CATALOG_GROUP_MEMBERS,
    CATALOG_GROUPS,
    _CatalogEntry,
    _entry_sources,
    refresh_catalog_groups,
)
from src.sqlite_store import SQLiteModelStore
from src.utils.auth import RequestSession

_COURSE_PREFIX = "/api/teach/course/"


class CatalogGroupsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteModelStore(Path(self.temp.name) / "group.sqlite")
        self.addCleanup(self.store.close)
        self.settings = patch.multiple(
            "src.catalog_groups",
            _CATALOG_ENTRIES=(
                _CatalogEntry("ma", "public", ("43",), "数学类", "Mathematics"),
                _CatalogEntry("pe", "public", ("66",), "体育类", "Physical Education"),
                _CatalogEntry("001", "department", ("2", "6"), "数学学院", "Math"),
                _CatalogEntry("other", "department", ("2",), "另一分类", "Other"),
                _CatalogEntry("quality", "quality", ("",), "综合素质类", "Quality"),
            ),
            _MAX_CONCURRENT_REQUESTS=2,
            _BATCH_INTERVAL_SECONDS=0,
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.responses = {
            "public/43": {
                "本科计划内课程": [
                    {"number": "001512", "valid": True, "category": "本科"}
                ],
                "空组": [],
            },
            "public/66": {
                "体育选项": {
                    "俱乐部课程": [
                        {"number": "*PE001", "valid": False, "lastTerm": None},
                        {"number": "*PE001", "valid": False, "lastTerm": None},
                    ],
                    "其他": [],
                    "未展开": {},
                }
            },
            "department/2": {
                "专业核心": [{"number": "001512", "gradation": "专业核心"}]
            },
            "department/6": [],
            "quality": {"": [{"number": "001512", "classify": "通识"}]},
        }
        self.paths: list[str] = []
        self.active_requests = 0
        self.max_active_requests = 0
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(self.client.aclose)
        self.session = RequestSession(client=self.client, page=None)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.host, "catalog.ustc.edu.cn")
        assert request.url.path.startswith(_COURSE_PREFIX)
        path = request.url.path.removeprefix(_COURSE_PREFIX)
        self.paths.append(path)
        self.active_requests += 1
        self.max_active_requests = max(self.active_requests, self.max_active_requests)
        try:
            await asyncio.sleep(0)
            return httpx.Response(200, json=self.responses[path])
        finally:
            self.active_requests -= 1

    async def test_persists_ordered_routes_nested_paths_and_original_records(
        self,
    ) -> None:
        await refresh_catalog_groups(self.session, self.store)
        self.assertCountEqual(self.paths, self.responses)
        self.assertEqual(self.max_active_requests, 2)
        self.assertEqual(
            self.store.conn.execute(
                "SELECT route_key, source_kind, source_id "
                f"FROM {CATALOG_GROUP_ENTRIES} "
                "ORDER BY route_position, source_position"
            ).fetchall(),
            [
                ("ma", "public", "43"),
                ("pe", "public", "66"),
                ("001", "department", "2"),
                ("001", "department", "6"),
                ("other", "department", "2"),
                ("quality", "quality", ""),
            ],
        )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT group_kind, member_count, display_label_cn "
                f"FROM {CATALOG_GROUPS} "
                "WHERE source_kind = ? AND source_id = ? AND group_path_json = ?",
                ("public", "66", '["体育选项","俱乐部课程"]'),
            ).fetchone(),
            ("array", 2, "体育选项 - 俱乐部课程"),
        )
        self.assertCountEqual(
            self.store.conn.execute(
                f"SELECT group_path_json, group_kind FROM {CATALOG_GROUPS} "
                "WHERE member_count = 0"
            ).fetchall(),
            [
                ('["空组"]', "array"),
                ('["体育选项"]', "object"),
                ('["体育选项","其他"]', "array"),
                ('["体育选项","未展开"]', "object"),
            ],
        )
        members = self.store.conn.execute(
            f"SELECT member_position, payload_json FROM {CATALOG_GROUP_MEMBERS} "
            "WHERE course_code = ? ORDER BY member_position",
            ("*PE001",),
        ).fetchall()
        expected = self.responses["public/66"]["体育选项"]["俱乐部课程"]
        self.assertEqual(
            [(i, json.loads(row)) for i, row in members], list(enumerate(expected))
        )
        self.assertEqual(
            self.store.conn.execute(
                f"SELECT count(*) FROM {CATALOG_GROUP_MEMBERS} WHERE course_code = ?",
                ("001512",),
            ).fetchone()[0],
            3,
        )
        self.assertCountEqual(
            self.store.conn.execute(
                f"SELECT group_key, display_label_cn FROM {CATALOG_GROUPS} "
                "WHERE source_kind IN ('department', 'quality')"
            ).fetchall(),
            [("专业核心", "专业核心课程"), ("", "其他单位")],
        )
        endpoints = self.store.conn.execute(
            "SELECT source_kind, source_id, payload_json "
            f"FROM {CATALOG_GROUP_ENDPOINTS}"
        ).fetchall()
        self.assertEqual(
            {
                f"{kind}/{key}".rstrip("/"): json.loads(payload)
                for kind, key, payload in endpoints
            },
            self.responses,
        )

    async def test_bad_response_does_not_replace_previous_snapshot(self) -> None:
        await refresh_catalog_groups(self.session, self.store)
        original = list(self.store.conn.iterdump())
        for invalid in (
            {"专业核心": [{"number": 1512}]},
            {"专业核心": "unexpected shape"},
            [{"number": "unlabelled"}],
        ):
            with self.subTest(invalid=invalid):
                self.responses["department/2"] = invalid
                with self.assertRaises(ValueError):
                    await refresh_catalog_groups(self.session, self.store)
                self.assertEqual(list(self.store.conn.iterdump()), original)

    async def test_failed_write_restores_previous_snapshot(self) -> None:
        await refresh_catalog_groups(self.session, self.store)
        original = list(self.store.conn.iterdump())
        store_response = self.store.store_response

        def fail_on_members(**kwargs):
            if kwargs["table_name"] == CATALOG_GROUP_MEMBERS:
                raise RuntimeError("write failure")
            return store_response(**kwargs)

        with (
            patch.object(self.store, "store_response", side_effect=fail_on_members),
            self.assertRaisesRegex(RuntimeError, "write failure"),
        ):
            await refresh_catalog_groups(self.session, self.store)
        self.assertEqual(list(self.store.conn.iterdump()), original)

    async def test_successful_refresh_replaces_old_groups_only(self) -> None:
        other_fetch = self.store.record_fetch(
            source="independent", method="GET", url="https://example.invalid"
        )
        self.store.conn.commit()
        await refresh_catalog_groups(self.session, self.store)
        self.responses["department/2"] = {"专业方向": []}
        await refresh_catalog_groups(self.session, self.store)
        self.assertEqual(
            self.store.conn.execute(
                f"SELECT group_key, member_count FROM {CATALOG_GROUPS} "
                "WHERE source_kind = 'department' AND source_id = '2'"
            ).fetchall(),
            [("专业方向", 0)],
        )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT count(*) FROM upstream_fetches WHERE source = ?",
                (CATALOG_GROUPS,),
            ).fetchone()[0],
            len(self.responses),
        )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT source FROM upstream_fetches WHERE id = ?", (other_fetch,)
            ).fetchone(),
            ("independent",),
        )

    async def test_entirely_empty_scope_is_rejected(self) -> None:
        self.responses = dict.fromkeys(self.responses, [])
        with self.assertRaisesRegex(ValueError, "no group memberships"):
            await refresh_catalog_groups(self.session, self.store)


class OfficialRouteRegistryTest(unittest.TestCase):
    def test_groups_include_compound_routes_and_vetted_department_ids(self) -> None:
        entries = [entry for rows in _entry_sources().values() for entry in rows]
        routes = {entry.route_key for entry in entries}
        self.assertEqual(len(routes), 23)
        self.assertEqual(len(_entry_sources()), 68)
        for key, kind, codes in [
            ("cs+es+in", "public", ["49", "41", "47", "102"]),
            ("pe", "public", ["66"]),
            ("001", "department", ["2", "5", "6", "61"]),
            ("208", "department", ["91", "18", "43", "46"]),
            ("quality", "quality", [""]),
        ]:
            route = sorted(
                (entry for entry in entries if entry.route_key == key),
                key=lambda entry: entry.source_position,
            )
            self.assertEqual({entry.source_kind for entry in route}, {kind})
            self.assertEqual([entry.source_id for entry in route], codes)
