import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from src.sqlite_store import SQLiteModelStore
from src.young import (
    SNAPSHOT_FILENAME,
    YOUNG_ACTIVE_ENDPOINT,
    YOUNG_ENDED_ENDPOINT,
    YOUNG_ENDED_SOURCE,
    _access_token_from_storage,
    _delete_young_source,
    _fetch_young_event_list,
    _store_young_event_payload,
    _young_api_url,
    make_young_events,
)


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
    ) -> None:
        self.pages = pages
        self.fail_endpoint = fail_endpoint
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
    def _write_snapshot(self, path: Path, *, item_name: str) -> None:
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
            finally:
                refreshed.close()
            self.assertEqual(row, ("ended-1", "new name"))

    async def test_fetch_failure_preserves_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            build_dir = Path(temporary_dir)
            snapshot_path = build_dir / SNAPSHOT_FILENAME
            self._write_snapshot(snapshot_path, item_name="usable snapshot")
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


if __name__ == "__main__":
    unittest.main()
