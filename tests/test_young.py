import unittest
from urllib.parse import parse_qs, urlsplit

from src.sqlite_store import SQLiteModelStore
from src.young import (
    YOUNG_ENDED_SOURCE,
    _access_token_from_storage,
    _cached_young_event_count,
    _delete_young_source,
    _fetch_young_event_list,
    _should_refresh_ended_events,
    _store_young_event_payload,
    _young_api_url,
)


class YoungEventCacheTest(unittest.TestCase):
    def test_refreshes_ended_events_when_cache_count_differs_from_total(self) -> None:
        self.assertTrue(_should_refresh_ended_events(cached_count=0, upstream_total=3))
        self.assertTrue(_should_refresh_ended_events(cached_count=2, upstream_total=3))
        self.assertFalse(_should_refresh_ended_events(cached_count=3, upstream_total=3))

    def test_counts_cached_event_records_for_source(self) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            fetch_id = store.record_fetch(
                source=YOUNG_ENDED_SOURCE,
                method="GET",
                url="https://young.ustc.edu.cn/endList",
            )
            store.store_json_response(
                table_name=YOUNG_ENDED_SOURCE,
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
                fetch_id=fetch_id,
            )

            count = _cached_young_event_count(store, YOUNG_ENDED_SOURCE)
            missing_count = _cached_young_event_count(store, "missing_source")
        finally:
            store.close()

        self.assertEqual(count, 2)
        self.assertEqual(missing_count, 0)

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
                records = (
                    [{"id": "1"}, {"id": "2"}]
                    if page_no == 1
                    else [{"id": "3"}]
                )
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
            [
                parse_qs(urlsplit(url).query)["pageNo"][0]
                for url in session.urls
            ],
            ["1", "2"],
        )

    def test_young_api_url_encodes_plain_query_params(self) -> None:
        self.assertEqual(
            _young_api_url("/mobile/item/endList", {"pageNo": 1, "pageSize": 3000}),
            "https://young.ustc.edu.cn/login/wisdom-group-learning-bg"
            "/mobile/item/endList?pageNo=1&pageSize=3000",
        )


if __name__ == "__main__":
    unittest.main()
