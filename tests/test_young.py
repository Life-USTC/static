import asyncio
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from main import _run_builders
from src.sqlite_store import SQLiteModelStore
from src.young import (
    SNAPSHOT_FILENAME,
    YOUNG_ACTIVE_ENDPOINT,
    YOUNG_ENDED_ENDPOINT,
    YOUNG_ENDED_SOURCE,
    YOUNG_NAMES_SOURCE,
    YOUNG_TREE_ENDPOINT,
    YoungReferencedNames,
    _access_token_from_storage,
    _delete_young_source,
    _fetch_young_event_list,
    _fetch_young_referenced_names,
    _referenced_tree_names,
    _refresh_young_referenced_names,
    _store_young_event_payload,
    _young_api_url,
    _young_referenced_ids,
    make_young_events,
)
from tools.verify_build import verify_build


def _payload(records: list[dict[str, object]], *, total: int | None = None):
    return {
        "success": True,
        "result": {
            "records": copy.deepcopy(records),
            "total": len(records) if total is None else total,
        },
    }


class FakeYoungSession:
    def __init__(
        self,
        pages: dict[str, list[dict[str, object]]],
        *,
        fail_endpoint: str | None = None,
        page=None,
    ) -> None:
        self.pages = pages
        self.fail_endpoint = fail_endpoint
        self.page = page
        self.timeout_ms = 1_000
        self.calls: list[tuple[str, int, int]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get_json(self, url: str):
        parsed = urlsplit(url)
        endpoint = parsed.path.removeprefix("/login/wisdom-group-learning-bg")
        params = parse_qs(parsed.query)
        page_no = int(params["pageNo"][0])
        page_size = int(params["pageSize"][0])
        self.calls.append((endpoint, page_no, page_size))
        if endpoint == self.fail_endpoint:
            raise RuntimeError("upstream unavailable")
        endpoint_pages = self.pages[endpoint]
        if page_no > len(endpoint_pages):
            raise AssertionError(f"unexpected page {page_no} for {endpoint}")
        return copy.deepcopy(endpoint_pages[page_no - 1])


class YoungEventStoreTest(unittest.TestCase):
    def test_store_young_event_payload_records_fetch_and_metadata(self) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            count = _store_young_event_payload(
                store,
                source=YOUNG_ENDED_SOURCE,
                endpoint="/mobile/item/endList",
                payload={
                    "success": True,
                    "result": {
                        "total": 2,
                        "records": [
                            {"id": "ended-1", "itemName": "Ended 1"},
                            {"id": "ended-2", "itemName": "Ended 2"},
                        ],
                    },
                },
                list_type="ended",
                page_size=3000,
            )

            fetch = store.conn.execute(
                """
                SELECT source, method, context
                FROM upstream_fetches
                """
            ).fetchone()
            metadata = dict(store.conn.execute("SELECT key, value FROM metadata"))
        finally:
            store.close()

        self.assertEqual(count, 2)
        self.assertEqual(
            fetch,
            (YOUNG_ENDED_SOURCE, "GET", "list_type=ended&page_size=3000"),
        )
        self.assertEqual(metadata[f"{YOUNG_ENDED_SOURCE}_record_count"], "2")
        self.assertEqual(metadata[f"{YOUNG_ENDED_SOURCE}_total"], "2")

    def test_delete_young_source_removes_previous_payload_rows(self) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            _store_young_event_payload(
                store,
                source=YOUNG_ENDED_SOURCE,
                endpoint="/mobile/item/endList",
                payload={
                    "success": True,
                    "result": {
                        "total": 1,
                        "records": [{"id": "ended-1", "itemName": "Ended 1"}],
                    },
                },
                list_type="ended",
                page_size=3000,
            )

            _delete_young_source(store, YOUNG_ENDED_SOURCE)

            fetch_count = store.conn.execute(
                "SELECT COUNT(*) FROM upstream_fetches"
            ).fetchone()[0]
            record_count = store.conn.execute(
                f"SELECT COUNT(*) FROM {YOUNG_ENDED_SOURCE}_result_records"
            ).fetchone()[0]
        finally:
            store.close()

        self.assertEqual(fetch_count, 0)
        self.assertEqual(record_count, 0)

    def test_access_token_from_storage_reads_vue_storage_value(self) -> None:
        self.assertEqual(
            _access_token_from_storage('{"value": "token-value", "expire": 123}'),
            "token-value",
        )


class YoungEventFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_pages_until_all_records_are_loaded(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.urls: list[str] = []

            async def get_json(self, url: str):
                self.urls.append(url)
                params = parse_qs(urlsplit(url).query)
                page_no = int(params["pageNo"][0])
                records = [{"id": "1"}, {"id": "2"}] if page_no == 1 else [{"id": "3"}]
                return {
                    "success": True,
                    "result": {
                        "records": records,
                        "total": 3,
                        "size": 2,
                        "current": page_no,
                        "pages": 2,
                    },
                }

        session = FakeSession()

        payload = await _fetch_young_event_list(
            session,  # type: ignore[arg-type]
            endpoint="/mobile/item/endList",
            page_size=2,
        )

        self.assertEqual(
            [record["id"] for record in payload["result"]["records"]],
            ["1", "2", "3"],
        )
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["pageNo"][0] for url in session.urls],
            ["1", "2"],
        )

    async def test_rejects_a_page_that_ends_before_total(self) -> None:
        session = FakeYoungSession(
            {
                YOUNG_ENDED_ENDPOINT: [
                    _payload([{"id": "1"}, {"id": "2"}], total=3),
                    _payload([], total=3),
                ]
            }
        )

        with self.assertRaises(ValueError):
            await _fetch_young_event_list(
                session,
                endpoint=YOUNG_ENDED_ENDPOINT,
                page_size=2,
            )

    async def test_rejects_malformed_records_in_a_page(self) -> None:
        session = FakeYoungSession(
            {
                YOUNG_ENDED_ENDPOINT: [
                    {
                        "success": True,
                        "result": {
                            "records": [{"id": "1"}, "not a record"],
                            "total": 2,
                        },
                    }
                ]
            }
        )

        with self.assertRaises(TypeError):
            await _fetch_young_event_list(
                session,
                endpoint=YOUNG_ENDED_ENDPOINT,
                page_size=2,
            )

    def test_young_api_url_encodes_plain_query_params(self) -> None:
        self.assertEqual(
            _young_api_url("/mobile/item/endList", {"pageNo": 1, "pageSize": 3000}),
            "https://young.ustc.edu.cn/login/wisdom-group-learning-bg"
            "/mobile/item/endList?pageNo=1&pageSize=3000",
        )


class YoungEventRefreshTest(unittest.IsolatedAsyncioTestCase):
    def _write_snapshot(
        self,
        path: Path,
        *,
        item_name: str,
        young_synced_at: str | None = None,
    ) -> None:
        store = SQLiteModelStore(path)
        try:
            _store_young_event_payload(
                store,
                source=YOUNG_ENDED_SOURCE,
                endpoint=YOUNG_ENDED_ENDPOINT,
                payload=_payload([{"id": "ended-1", "itemName": item_name}]),
                list_type="ended",
                page_size=3000,
            )
            store.put_metadata(
                {
                    "young_events_mode": "full",
                    **(
                        {"young_events_synced_at": young_synced_at}
                        if young_synced_at is not None
                        else {}
                    ),
                }
            )
        finally:
            store.close()

    async def test_refreshes_ended_content_when_total_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            build_dir = Path(temporary_dir)
            snapshot_path = build_dir / SNAPSHOT_FILENAME
            self._write_snapshot(snapshot_path, item_name="old name")
            session = FakeYoungSession(
                {
                    YOUNG_ACTIVE_ENDPOINT: [
                        _payload([{"id": "active-1", "itemName": "Active"}])
                    ],
                    YOUNG_ENDED_ENDPOINT: [
                        _payload([{"id": "ended-1", "itemName": "new name"}])
                    ],
                }
            )

            with (
                patch("src.young.BUILD_DIR", build_dir),
                patch(
                    "src.young.USTCSession",
                    lambda **_: session,
                ),
                patch("src.young._prepare_young_session", new=AsyncMock()),
            ):
                await make_young_events()

            self.assertEqual(
                session.calls,
                [
                    (YOUNG_ACTIVE_ENDPOINT, 1, 3000),
                    (YOUNG_ENDED_ENDPOINT, 1, 3000),
                ],
            )
            refreshed = SQLiteModelStore(snapshot_path, reset=False)
            try:
                row = refreshed.conn.execute(
                    f"SELECT id, itemName FROM {YOUNG_ENDED_SOURCE}_result_records"
                ).fetchone()
                metadata = dict(
                    refreshed.conn.execute("SELECT key, value FROM metadata")
                )
            finally:
                refreshed.close()
            self.assertEqual(row, ("ended-1", "new name"))
            self.assertEqual(metadata["young_events_mode"], "full")
            synced_at = datetime.fromisoformat(metadata["young_events_synced_at"])
            self.assertIsNotNone(synced_at.tzinfo)
            self.assertEqual(synced_at.utcoffset(), UTC.utcoffset(synced_at))

    async def test_curriculum_only_does_not_bump_young_synced_at(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            snapshot_path = Path(temporary_dir) / SNAPSHOT_FILENAME
            status_path = Path(temporary_dir) / "build-status.json"
            previous_synced_at = "2026-09-14T08:00:00.000000+00:00"
            self._write_snapshot(
                snapshot_path,
                item_name="cached snapshot",
                young_synced_at=previous_synced_at,
            )

            async def curriculum_builder() -> None:
                curriculum_store = SQLiteModelStore(snapshot_path, reset=False)
                curriculum_store.close()

            await _run_builders(
                [("curriculum", curriculum_builder, (snapshot_path,))],
                status_path=status_path,
            )

            refreshed = SQLiteModelStore(snapshot_path, reset=False)
            try:
                metadata = dict(
                    refreshed.conn.execute("SELECT key, value FROM metadata")
                )
            finally:
                refreshed.close()
            self.assertEqual(
                metadata["young_events_synced_at"],
                previous_synced_at,
            )

    async def test_fetch_failure_preserves_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            build_dir = Path(temporary_dir)
            snapshot_path = build_dir / SNAPSHOT_FILENAME
            previous_synced_at = "2026-09-14T08:00:00.000000+00:00"
            self._write_snapshot(
                snapshot_path,
                item_name="usable snapshot",
                young_synced_at=previous_synced_at,
            )
            previous_bytes = snapshot_path.read_bytes()
            session = FakeYoungSession(
                {
                    YOUNG_ACTIVE_ENDPOINT: [_payload([])],
                    YOUNG_ENDED_ENDPOINT: [],
                },
                fail_endpoint=YOUNG_ENDED_ENDPOINT,
            )

            with (
                patch("src.young.BUILD_DIR", build_dir),
                patch(
                    "src.young.USTCSession",
                    lambda **_: session,
                ),
                patch("src.young._prepare_young_session", new=AsyncMock()),
                self.assertRaises(RuntimeError),
            ):
                await make_young_events()

            self.assertEqual(snapshot_path.read_bytes(), previous_bytes)
            preserved = SQLiteModelStore(snapshot_path, reset=False)
            try:
                metadata = dict(
                    preserved.conn.execute("SELECT key, value FROM metadata")
                )
            finally:
                preserved.close()
            self.assertEqual(
                metadata["young_events_synced_at"],
                previous_synced_at,
            )


def _organization_node(source_id, title, parent="", children=None):
    node = {
        "id": source_id,
        "key": source_id,
        "value": source_id,
        "title": title,
        "departName": title,
        "parentId": parent,
        "createBy": "PRIVATE_CREATOR_NOT_FOR_PUBLICATION",
        "guideTeachers": "PRIVATE_TEACHER_NOT_FOR_PUBLICATION",
    }
    if children is not None:
        node["children"] = children
    return node


def _organization_payload():
    return {
        "success": True,
        "result": [
            _organization_node(
                "unreferenced-parent",
                "UNREFERENCED_PARENT_NAME",
                children=[
                    _organization_node("one", "Same name", "unreferenced-parent"),
                    _organization_node("two", "Same name", "unreferenced-parent"),
                    _organization_node(
                        "opaque,'\\,Ａ", "Comma,name，kept", "unreferenced-parent"
                    ),
                    _organization_node(
                        "unreferenced-sibling",
                        "UNREFERENCED_SIBLING_NAME",
                        "unreferenced-parent",
                    ),
                ],
            )
        ],
    }


class FakeTreeResponse:
    url = (
        "https://young.ustc.edu.cn/login/wisdom-group-learning-bg" + YOUNG_TREE_ENDPOINT
    )
    status = 200

    def __init__(self, raw):
        self.raw = raw

    async def body(self):
        return self.raw


class FakeTreePage:
    def __init__(self, payload=None, *, fail=False):
        self.raw = json.dumps(
            payload if payload is not None else _organization_payload(),
            ensure_ascii=False,
            indent=2,
        ).encode()
        self.fail = fail
        self.calls = []
        self.response_observers = 0

    async def wait_for_function(self, expression, **kwargs):
        self.readiness = (expression, kwargs)

    def expect_response(self, predicate, **_kwargs):
        response = FakeTreeResponse(self.raw)
        if not predicate(response):
            raise AssertionError("Expected the fixed organization endpoint")
        self.response_observers += 1

        class Pending:
            @property
            async def value(self):
                return response

        class ResponseContext:
            async def __aenter__(self):
                return Pending()

            async def __aexit__(self, *_args):
                return False

        return ResponseContext()

    async def evaluate(self, expression, **kwargs):
        self.calls.append((expression, kwargs))
        if self.fail:
            raise RuntimeError("simulated directory failure")
        return True


def _names_rows(store):
    return store.conn.execute(
        "SELECT id,title,fetch_id FROM young_referenced_organization_names ORDER BY id"
    ).fetchall()


def _names_metadata(store):
    return dict(
        store.conn.execute(
            "SELECT key,value FROM metadata WHERE key LIKE 'young_names_%'"
        )
    )


def _seed_names(store, names):
    _refresh_young_referenced_names(
        store,
        set(names),
        complete=True,
        observation=YoungReferencedNames(names, "a" * 64, "2030-01-01T00:00:00+00:00"),
        status="unused",
    )


class YoungReferencedNamesTest(unittest.IsolatedAsyncioTestCase):
    def test_references_keep_opaque_arrays_and_only_split_id_csv(self):
        records = [
            {"organizer": ["opaque,'\\,Ａ", "Case", "Case"], "sponsor": " two ,one"},
            {"organizer": "", "sponsor": []},
        ]
        ids, complete = _young_referenced_ids(records)
        self.assertTrue(complete)
        self.assertEqual(ids, {"opaque,'\\,Ａ", "Case", "one", "two"})
        for value in (None, " ", ",", "a,,b", ["ok", ""], ["ok", 1]):
            with self.subTest(value=value):
                _, complete = _young_referenced_ids(
                    [{"organizer": value, "sponsor": ""}]
                )
                self.assertFalse(complete)

    def test_tree_only_projects_exact_references_without_name_merging(self):
        self.assertEqual(
            _referenced_tree_names(_organization_payload(), {"one", "two", "missing"}),
            {"one": "Same name", "two": "Same name"},
        )
        opaque = "opaque,'\\,Ａ"
        self.assertEqual(
            _referenced_tree_names(_organization_payload(), {opaque}),
            {opaque: "Comma,name，kept"},
        )
        variants = []
        bad = _organization_payload()
        bad["result"][0]["children"][0]["parentId"] = "wrong-parent"
        variants.append(bad)
        bad = _organization_payload()
        bad["result"][0]["children"][0]["key"] = "different-key"
        variants.append(bad)
        bad = _organization_payload()
        bad["result"][0]["children"].append(copy.deepcopy(bad["result"][0]))
        variants.append(bad)
        bad = _organization_payload()
        bad["result"][0]["parentId"] = "one"
        variants.append(bad)
        variants.append({"success": True, "result": []})
        for bad in variants:
            with self.subTest(shape="invalid"), self.assertRaises(ValueError):
                _referenced_tree_names(bad, {"one"})

    async def test_one_real_helper_contract_hashes_original_http_bytes(self):
        page = FakeTreePage()
        session = FakeYoungSession({}, page=page)
        observation = await _fetch_young_referenced_names(session, {"one", "two"})
        self.assertEqual(observation.names, {"one": "Same name", "two": "Same name"})
        self.assertEqual(
            observation.response_sha256, hashlib.sha256(page.raw).hexdigest()
        )
        self.assertNotEqual(
            observation.response_sha256,
            hashlib.sha256(json.dumps(json.loads(page.raw)).encode()).hexdigest(),
        )
        self.assertEqual(page.response_observers, 1)
        self.assertEqual(len(page.calls), 1)
        self.assertEqual(page.calls[0][1], {"isolated_context": False})
        self.assertIn(
            "vm.$http.get('/sysdepart/sysDepart/queryTreeList')", page.calls[0][0]
        )
        self.assertIsNotNone(datetime.fromisoformat(observation.observed_at).tzinfo)

    def test_positive_names_preserve_referenced_unmatched_provenance(self):
        store = SQLiteModelStore(":memory:")
        try:
            _seed_names(
                store,
                {"one": "old one", "missing": "cached missing", "gone": "old gone"},
            )
            old = {row[0]: row for row in _names_rows(store)}
            _refresh_young_referenced_names(
                store,
                {"one", "missing", "unknown"},
                complete=True,
                observation=YoungReferencedNames(
                    {"one": "new one"}, "b" * 64, "2030-01-02T00:00:00+00:00"
                ),
                status="unused",
            )
            rows = {row[0]: row for row in _names_rows(store)}
            self.assertEqual(set(rows), {"one", "missing"})
            self.assertEqual(rows["missing"], old["missing"])
            self.assertEqual(rows["one"][1], "new one")
            self.assertNotEqual(rows["one"][2], old["one"][2])
            self.assertEqual(
                _names_metadata(store)["young_names_last_attempt"], "partial"
            )
            fetches = store.conn.execute(
                "SELECT id,source FROM upstream_fetches ORDER BY id"
            ).fetchall()
            self.assertEqual(len(fetches), 2)
            self.assertTrue(all(row[1] == YOUNG_NAMES_SOURCE for row in fetches))
        finally:
            store.close()

    async def test_partial_references_add_only_known_positives_and_keep_old_cohort(
        self,
    ):
        for cached in (False, True):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as tmp:
                build = Path(tmp)
                previous = []
                if cached:
                    store = SQLiteModelStore(build / SNAPSHOT_FILENAME)
                    _seed_names(store, {"one": "old one", "gone": "old gone"})
                    previous = _names_rows(store)
                    store.close()
                page = FakeTreePage()
                session = FakeYoungSession(
                    {
                        YOUNG_ACTIVE_ENDPOINT: [
                            _payload(
                                [
                                    {"id": "partial", "organizer": ["two"]},
                                    {
                                        "id": "unknown-id",
                                        "organizer": "unknown",
                                        "sponsor": "",
                                    },
                                ]
                            )
                        ],
                        YOUNG_ENDED_ENDPOINT: [_payload([])],
                    },
                    page=page,
                )
                with (
                    patch("src.young.BUILD_DIR", build),
                    patch(
                        "src.young.USTCSession", lambda session=session, **_: session
                    ),
                    patch("src.young._prepare_young_session", new=AsyncMock()),
                    self.assertLogs("src.young", level="INFO") as recorded,
                ):
                    await make_young_events()
                self.assertEqual(len(page.calls), 1)
                self.assertTrue(
                    any(
                        "referenceComplete=False referencedCount=2 "
                        "freshMatchedCount=1 status=partial" in line
                        for line in recorded.output
                    )
                )
                store = SQLiteModelStore(build / SNAPSHOT_FILENAME, reset=False)
                try:
                    rows = {row[0]: row for row in _names_rows(store)}
                    self.assertEqual(set(rows), {row[0] for row in previous} | {"two"})
                    for row in previous:
                        self.assertEqual(rows[row[0]], row)
                    self.assertEqual(rows["two"][1], "Same name")
                    if previous:
                        self.assertNotEqual(rows["two"][2], previous[0][2])
                    metadata = _names_metadata(store)
                    self.assertEqual(metadata["young_names_reference_complete"], "0")
                    self.assertEqual(metadata["young_names_reference_count"], "2")
                    self.assertEqual(metadata["young_names_last_attempt"], "partial")
                    self.assertEqual(
                        metadata["young_names_response_sha256"],
                        hashlib.sha256(page.raw).hexdigest(),
                    )
                    dump = "\n".join(store.conn.iterdump())
                    for forbidden in (
                        "UNREFERENCED_PARENT_NAME",
                        "UNREFERENCED_SIBLING_NAME",
                        "PRIVATE_CREATOR_NOT_FOR_PUBLICATION",
                        "PRIVATE_TEACHER_NOT_FOR_PUBLICATION",
                        "parentId",
                    ):
                        self.assertNotIn(forbidden, dump)
                finally:
                    store.close()

    def test_failed_fresh_write_rolls_back_after_delete_but_keeps_privacy_prune(self):
        store = SQLiteModelStore(":memory:")
        try:
            _seed_names(store, {"one": "old one", "gone": "old gone"})
            old = {row[0]: row for row in _names_rows(store)}
            metadata = _names_metadata(store)
            original = store.put_metadata
            saw_deleted_fetch = False

            def fail_after_orphan_delete(items):
                nonlocal saw_deleted_fetch
                if items.get("young_names_response_sha256") == "b" * 64:
                    saw_deleted_fetch = (
                        store.conn.execute(
                            "SELECT id FROM upstream_fetches WHERE id = ?",
                            (old["one"][2],),
                        ).fetchone()
                        is None
                    )
                    raise RuntimeError(
                        "simulated failure after deleting an orphan fetch"
                    )
                original(items)

            _store_young_event_payload(
                store,
                source=YOUNG_ENDED_SOURCE,
                endpoint=YOUNG_ENDED_ENDPOINT,
                payload=_payload([{"id": "event", "itemName": "new healthy event"}]),
                list_type="ended",
                page_size=3000,
            )
            with patch.object(
                store, "put_metadata", side_effect=fail_after_orphan_delete
            ):
                _refresh_young_referenced_names(
                    store,
                    {"one"},
                    complete=True,
                    observation=YoungReferencedNames(
                        {"one": "new one"}, "b" * 64, "2030-01-02T00:00:00+00:00"
                    ),
                    status="unused",
                )
            self.assertTrue(saw_deleted_fetch)
            self.assertEqual(_names_rows(store), [old["one"]])
            self.assertEqual(
                store.conn.execute(
                    "SELECT source FROM upstream_fetches WHERE id = ?", (old["one"][2],)
                ).fetchone(),
                (YOUNG_NAMES_SOURCE,),
            )
            final = _names_metadata(store)
            self.assertEqual(
                final["young_names_response_sha256"],
                metadata["young_names_response_sha256"],
            )
            self.assertEqual(
                final["young_names_observed_at"], metadata["young_names_observed_at"]
            )
            self.assertEqual(final["young_names_last_attempt"], "write_failed")
            self.assertEqual(
                store.conn.execute(
                    f"SELECT itemName FROM {YOUNG_ENDED_SOURCE}_result_records"
                ).fetchall(),
                [("new healthy event",)],
            )
        finally:
            store.close()

    async def test_successful_public_snapshot_contains_only_referenced_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            opaque = "opaque,'\\,Ａ"
            session = FakeYoungSession(
                {
                    YOUNG_ACTIVE_ENDPOINT: [
                        _payload(
                            [
                                {
                                    "id": "active",
                                    "organizer": ["one", opaque],
                                    "sponsor": "two",
                                }
                            ]
                        )
                    ],
                    YOUNG_ENDED_ENDPOINT: [_payload([])],
                },
                page=FakeTreePage(),
            )
            with (
                patch("src.young.BUILD_DIR", build),
                patch("src.young.USTCSession", lambda **_: session),
                patch("src.young._prepare_young_session", new=AsyncMock()),
            ):
                await make_young_events()
            store = SQLiteModelStore(build / SNAPSHOT_FILENAME, reset=False)
            try:
                self.assertEqual(
                    {row[0]: row[1] for row in _names_rows(store)},
                    {
                        "one": "Same name",
                        "two": "Same name",
                        opaque: "Comma,name，kept",
                    },
                )
                columns = [
                    row[1]
                    for row in store.conn.execute(
                        "PRAGMA table_info(young_referenced_organization_names)"
                    )
                ]
                self.assertEqual(columns, ["id", "title", "fetch_id"])
                dump = "\n".join(store.conn.iterdump())
                for forbidden in [
                    "UNREFERENCED_PARENT_NAME",
                    "UNREFERENCED_SIBLING_NAME",
                    "PRIVATE_CREATOR_NOT_FOR_PUBLICATION",
                    "PRIVATE_TEACHER_NOT_FOR_PUBLICATION",
                    "guideTeachers",
                    "parentId",
                ]:
                    self.assertNotIn(forbidden, dump)
                event_fetches = store.conn.execute(
                    "SELECT source FROM upstream_fetches WHERE source != ?",
                    (YOUNG_NAMES_SOURCE,),
                ).fetchall()
                self.assertEqual(
                    {row[0] for row in event_fetches},
                    {"young_mobile_item_enrolment_list", YOUNG_ENDED_SOURCE},
                )
                metadata = _names_metadata(store)
                self.assertEqual(
                    metadata["young_names_response_sha256"],
                    hashlib.sha256(session.page.raw).hexdigest(),
                )
                self.assertEqual(metadata["young_names_last_attempt"], "ok")
                accepted = dict(metadata)
            finally:
                store.close()
            # Opening the same SQLite from an unrelated builder cannot advance
            # the dictionary observation or any cached name's provenance.
            unrelated = SQLiteModelStore(build / SNAPSHOT_FILENAME, reset=False)
            self.assertEqual(_names_metadata(unrelated), accepted)
            unrelated.close()

    async def test_bad_directory_keeps_healthy_events_on_cached_and_cold_starts(self):
        for cached in (False, True):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as tmp:
                build = Path(tmp)
                before = None
                if cached:
                    store = SQLiteModelStore(build / SNAPSHOT_FILENAME)
                    _seed_names(store, {"one": "cached one", "gone": "must prune"})
                    before = {row[0]: row for row in _names_rows(store)}
                    accepted = _names_metadata(store)
                    store.close()
                session = FakeYoungSession(
                    {
                        YOUNG_ACTIVE_ENDPOINT: [
                            _payload(
                                [
                                    {
                                        "id": "fresh",
                                        "itemName": "healthy event",
                                        "organizer": "one",
                                        "sponsor": "unknown",
                                    }
                                ]
                            )
                        ],
                        YOUNG_ENDED_ENDPOINT: [_payload([])],
                    },
                    page=FakeTreePage(fail=True),
                )
                with (
                    patch("src.young.BUILD_DIR", build),
                    patch(
                        "src.young.USTCSession", lambda session=session, **_: session
                    ),
                    patch("src.young._prepare_young_session", new=AsyncMock()),
                ):
                    await make_young_events()
                store = SQLiteModelStore(build / SNAPSHOT_FILENAME, reset=False)
                try:
                    self.assertEqual(
                        store.conn.execute(
                            "SELECT itemName FROM "
                            "young_mobile_item_enrolment_list_result_records"
                        ).fetchall(),
                        [("healthy event",)],
                    )
                    self.assertEqual(
                        _names_metadata(store)["young_names_last_attempt"],
                        "fetch_failed",
                    )
                    if cached:
                        self.assertEqual(_names_rows(store), [before["one"]])
                        self.assertEqual(
                            _names_metadata(store)["young_names_observed_at"],
                            accepted["young_names_observed_at"],
                        )
                    else:
                        self.assertEqual(_names_rows(store), [])
                        self.assertNotIn(
                            "young_names_observed_at", _names_metadata(store)
                        )
                finally:
                    store.close()

    async def test_real_sqlite_rejection_does_not_publish_the_staged_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            snapshot = build / SNAPSHOT_FILENAME
            store = SQLiteModelStore(snapshot)
            _seed_names(store, {"one": "cached one"})
            store.conn.execute(
                """
                CREATE TRIGGER reject_new_organization_name
                BEFORE INSERT ON young_referenced_organization_names
                BEGIN SELECT RAISE(ABORT, 'synthetic storage rejection'); END
                """
            )
            store.close()
            original = snapshot.read_bytes()
            session = FakeYoungSession(
                {
                    YOUNG_ACTIVE_ENDPOINT: [
                        _payload([{"id": "fresh", "organizer": "one", "sponsor": ""}])
                    ],
                    YOUNG_ENDED_ENDPOINT: [_payload([])],
                },
                page=FakeTreePage(),
            )
            with (
                patch("src.young.BUILD_DIR", build),
                patch("src.young.USTCSession", lambda **_: session),
                patch("src.young._prepare_young_session", new=AsyncMock()),
                self.assertRaisesRegex(sqlite3.IntegrityError, "storage rejection"),
            ):
                await make_young_events()
            self.assertEqual(snapshot.read_bytes(), original)

    async def test_generated_optional_names_table_passes_existing_build_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / "build"
            session = FakeYoungSession(
                {
                    YOUNG_ACTIVE_ENDPOINT: [
                        _payload([{"id": "fresh", "organizer": "one", "sponsor": ""}])
                    ],
                    YOUNG_ENDED_ENDPOINT: [_payload([])],
                },
                page=FakeTreePage(),
            )
            with (
                patch("src.young.BUILD_DIR", build),
                patch("src.young.USTCSession", lambda **_: session),
                patch("src.young._prepare_young_session", new=AsyncMock()),
            ):
                await make_young_events()
            store = SQLiteModelStore(build / SNAPSHOT_FILENAME, reset=False)
            self.assertEqual(len(_names_rows(store)), 1)
            store.close()
            schemas = build / "schemas" / "upstream"
            schemas.mkdir(parents=True)
            for index in range(7):
                (schemas / f"schema-{index}.expected.schema.json").write_text("{}")
            (build / "rss").mkdir()
            (build / "rss" / "feed.xml").write_text("<rss />")
            (build / "imgs" / "rooms").mkdir(parents=True)
            (build / "imgs" / "rooms" / "3A201.png").write_bytes(b"png")
            (build / "room_maps.json").write_text(
                json.dumps(
                    {
                        "rooms": [
                            {
                                "code": "3A201",
                                "building": "三教主",
                                "floor": "2",
                                "imagePath": "imgs/rooms/3A201.png",
                                "sourceImagePath": "imgs/三教主_02.png",
                            }
                        ]
                    }
                )
            )
            status = build / "build-status.json"
            status.write_text(
                json.dumps(
                    {
                        "builders": {
                            "curriculum": {"status": "failed"},
                            "young": {"status": "ok"},
                            "rss": {"status": "ok"},
                            "blackboard": {"status": "failed"},
                        }
                    }
                )
            )
            verify_build(
                build_dir=build,
                status_path=status,
                diagnostic_dir=root / ".artifacts" / "upstream-contracts",
            )

    async def test_hanging_optional_fetch_is_cancelled_without_blocking_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            store = SQLiteModelStore(build / SNAPSHOT_FILENAME)
            _seed_names(store, {"one": "cached one"})
            previous = _names_rows(store)
            accepted = _names_metadata(store)
            store.close()
            session = FakeYoungSession(
                {
                    YOUNG_ACTIVE_ENDPOINT: [
                        _payload(
                            [
                                {
                                    "id": "fresh",
                                    "itemName": "healthy after cancellation",
                                    "organizer": "one",
                                    "sponsor": "",
                                }
                            ]
                        )
                    ],
                    YOUNG_ENDED_ENDPOINT: [_payload([])],
                }
            )
            session.timeout_ms = 10
            cancelled = False

            async def never_settle(_session, references):
                nonlocal cancelled
                self.assertIs(_session, session)
                self.assertEqual(references, {"one"})
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled = True

            with (
                patch("src.young.BUILD_DIR", build),
                patch("src.young.USTCSession", lambda **_: session),
                patch("src.young._prepare_young_session", new=AsyncMock()),
                patch("src.young._fetch_young_referenced_names", new=never_settle),
            ):
                await make_young_events()
            self.assertTrue(cancelled)
            refreshed = SQLiteModelStore(build / SNAPSHOT_FILENAME, reset=False)
            try:
                self.assertEqual(_names_rows(refreshed), previous)
                metadata = _names_metadata(refreshed)
                self.assertEqual(metadata["young_names_last_attempt"], "fetch_failed")
                self.assertEqual(
                    metadata["young_names_observed_at"],
                    accepted["young_names_observed_at"],
                )
                self.assertEqual(
                    refreshed.conn.execute(
                        "SELECT itemName FROM "
                        "young_mobile_item_enrolment_list_result_records"
                    ).fetchall(),
                    [("healthy after cancellation",)],
                )
            finally:
                refreshed.close()

    async def test_event_failure_preserves_previous_dictionary_snapshot_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            snapshot = build / SNAPSHOT_FILENAME
            store = SQLiteModelStore(snapshot)
            _seed_names(store, {"one": "cached one"})
            store.close()
            before = snapshot.read_bytes()
            page = FakeTreePage()
            session = FakeYoungSession(
                {
                    YOUNG_ACTIVE_ENDPOINT: [_payload([])],
                    YOUNG_ENDED_ENDPOINT: [],
                },
                fail_endpoint=YOUNG_ENDED_ENDPOINT,
                page=page,
            )
            with (
                patch("src.young.BUILD_DIR", build),
                patch("src.young.USTCSession", lambda **_: session),
                patch("src.young._prepare_young_session", new=AsyncMock()),
                self.assertRaises(RuntimeError),
            ):
                await make_young_events()
            self.assertEqual(snapshot.read_bytes(), before)
            self.assertEqual(page.calls, [])


if __name__ == "__main__":
    unittest.main()
