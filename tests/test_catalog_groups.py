import json
import unittest
from unittest.mock import AsyncMock

from src.catalog_groups import (
    CATALOG_GROUP_ENDPOINTS,
    CATALOG_GROUP_ENTRIES,
    CATALOG_GROUP_MEMBERS,
    CATALOG_GROUPS,
    refresh_catalog_groups,
)
from src.sqlite_store import SQLiteModelStore


def route(code, ids):
    name = {"cn": code, "en": code}
    return {"code": code, "id": ids, "type": "generic", "name": name}


def browser_bundle(entries):
    registry = f"const routes = {json.dumps(entries)};"
    return (
        registry
        + """
    switch (kind) {
        case 'school': endpoint='department'; break;
        case 'generic': endpoint='public'; break;
        case 'quality': endpoint='quality'; break;
    }
    const generic = routes.find(x=>x.code===route).id.split("+");
    const school = routes.find(x=>x.code===route).id.split("+");
    const indexed = `/api/teach/course/${endpoint}/${id}`;
    const department = `/api/teach/course/department/${id}`;
    const unindexed = `/api/teach/course/${endpoint}`;
    """
    )


class CatalogGroupsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SQLiteModelStore(":memory:")
        self.addCleanup(self.store.close)
        self.entries = [route("math", "66+43"), route("alias", "66")]
        self.confirmed = self.entries
        self.requests = []
        self.payload = {"体育选项": {"俱乐部": [{"number": "001512"}] * 2, "空": {}}}

    async def get(self, url, headers=None, **_kwargs):
        body = '<script type="module" src="/assets/index-new.js"></script>'
        if not url.endswith("/catalog"):
            entries = self.confirmed if headers else self.entries
            if isinstance(entries, Exception):
                raise entries
            body = browser_bundle(entries)
        response = AsyncMock()
        response.url, response.text.return_value = url, body
        return response

    async def get_json(self, url, **_kwargs):
        id_ = url.rsplit("/", 1)[-1]
        self.requests.append(id_)
        return self.payload if id_ == "66" else []

    def read(self, query):
        return self.store.conn.execute(query).fetchall()

    async def test_new_route_preserves_ordered_groups_and_raw_records(self):
        self.entries.append(route("new", "777"))
        await refresh_catalog_groups(self, self.store)
        self.assertEqual(self.requests, ["66", "43", "777"])
        routes = self.read(
            f"SELECT route_key, source_id FROM {CATALOG_GROUP_ENTRIES} "
            "ORDER BY route_position, source_position"
        )
        self.assertEqual(
            routes, [("math", "66"), ("math", "43"), ("alias", "66"), ("new", "777")]
        )
        groups = self.read(f"SELECT group_kind FROM {CATALOG_GROUPS} ORDER BY position")
        self.assertEqual(groups, [("object",), ("array",), ("object",)])
        members = self.read(f"SELECT course_code FROM {CATALOG_GROUP_MEMBERS}")
        self.assertEqual(members, [("001512",)] * 2)
        payload = self.read(
            f"SELECT payload_json FROM {CATALOG_GROUP_ENDPOINTS} WHERE source_id = '66'"
        )[0][0]
        self.assertEqual(json.loads(payload), self.payload)

    async def test_shrink_requires_matching_discovery_and_valid_group_data(self):
        await refresh_catalog_groups(self, self.store)
        dump = self.store.conn.iterdump
        snapshot = list(dump())
        self.entries = [route("math", "66")]
        self.requests.clear()
        for confirmation in ([route("other", "66")], RuntimeError("offline")):
            self.confirmed = confirmation
            with self.assertRaises((RuntimeError, ValueError)):
                await refresh_catalog_groups(self, self.store)
            self.assertEqual(self.requests, [])
            self.assertEqual(list(dump()), snapshot)

        self.confirmed = self.entries
        original, self.payload = self.payload, {"broken": [None]}
        with self.assertRaises(ValueError):
            await refresh_catalog_groups(self, self.store)
        self.assertEqual(list(dump()), snapshot)
        self.payload = original
        await refresh_catalog_groups(self, self.store)
        self.assertEqual(
            self.read(f"SELECT route_key FROM {CATALOG_GROUP_ENTRIES}"), [("math",)]
        )
