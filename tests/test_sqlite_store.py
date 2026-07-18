import tempfile
import unittest
from pathlib import Path

from src.sqlite_store import SQLiteModelStore


class SQLiteModelStoreCacheTest(unittest.TestCase):
    def test_can_reopen_existing_store_without_resetting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.sqlite"
            store = SQLiteModelStore(path)
            store.record_fetch(
                source="source", method="GET", url="https://example.test"
            )
            store.close()

            reopened = SQLiteModelStore(path, reset=False)
            try:
                count = reopened.conn.execute(
                    "SELECT COUNT(*) FROM upstream_fetches WHERE source = 'source'"
                ).fetchone()[0]
            finally:
                reopened.close()

        self.assertEqual(count, 1)

    def test_delete_fetches_removes_rows_from_all_fetch_id_tables(self) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            keep_id = store.record_fetch(
                source="source",
                method="GET",
                url="https://example.test/keep",
            )
            delete_id = store.record_fetch(
                source="source",
                method="GET",
                url="https://example.test/delete",
            )
            store.conn.execute(
                "CREATE TABLE cached_rows("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "fetch_id INTEGER NOT NULL, "
                "value TEXT NOT NULL)"
            )
            store.conn.executemany(
                "INSERT INTO cached_rows(fetch_id, value) VALUES(?, ?)",
                [(keep_id, "keep"), (delete_id, "delete")],
            )

            store.delete_fetches([delete_id])

            fetches = store.conn.execute(
                "SELECT id FROM upstream_fetches ORDER BY id"
            ).fetchall()
            rows = store.conn.execute(
                "SELECT fetch_id, value FROM cached_rows ORDER BY fetch_id"
            ).fetchall()
        finally:
            store.close()

        self.assertEqual(fetches, [(keep_id,)])
        self.assertEqual(rows, [(keep_id, "keep")])

    def test_store_json_response_preserves_nested_upstream_fields(self) -> None:
        store = SQLiteModelStore(":memory:")
        try:
            fetch_id = store.record_fetch(
                source="young_mobile_item_enrolment_list",
                method="GET",
                url="https://young.ustc.edu.cn/list",
                context={"list_type": "active"},
            )

            count = store.store_json_response(
                table_name="young_mobile_item_enrolment_list",
                payload={
                    "success": True,
                    "code": 200,
                    "result": {
                        "total": 1,
                        "records": [
                            {
                                "id": "event-1",
                                "itemName": "Event",
                                "budgetList": [
                                    {"budgetName": "materials", "amount": 12.5}
                                ],
                                "itemPlaceDTO": {"placeInfo": "Room 101"},
                                "regOptions": ["student", "teacher"],
                            }
                        ],
                    },
                },
                fetch_id=fetch_id,
                context={"list_type": "active"},
            )

            root = store.conn.execute(
                """
                SELECT success, code, list_type
                FROM young_mobile_item_enrolment_list
                """
            ).fetchone()
            result = store.conn.execute(
                """
                SELECT total, list_type
                FROM young_mobile_item_enrolment_list_result
                """
            ).fetchone()
            record = store.conn.execute(
                """
                SELECT id, itemName, list_type
                FROM young_mobile_item_enrolment_list_result_records
                """
            ).fetchone()
            budget = store.conn.execute(
                """
                SELECT budgetName, amount, list_type
                FROM young_mobile_item_enrolment_list_result_records_budgetList
                """
            ).fetchone()
            place = store.conn.execute(
                """
                SELECT placeInfo, list_type
                FROM young_mobile_item_enrolment_list_result_records_itemPlaceDTO
                """
            ).fetchone()
            reg_options = store.conn.execute(
                """
                SELECT value, position, list_type
                FROM young_mobile_item_enrolment_list_result_records_regOptions
                ORDER BY position
                """
            ).fetchall()

            store.delete_fetches([fetch_id])
            remaining_records = store.conn.execute(
                """
                SELECT COUNT(*)
                FROM young_mobile_item_enrolment_list_result_records
                """
            ).fetchone()[0]
        finally:
            store.close()

        self.assertEqual(count, 1)
        self.assertEqual(root, (1, 200, "active"))
        self.assertEqual(result, (1, "active"))
        self.assertEqual(record, ("event-1", "Event", "active"))
        self.assertEqual(budget, ("materials", 12.5, "active"))
        self.assertEqual(place, ("Room 101", "active"))
        self.assertEqual(
            reg_options,
            [("student", 0, "active"), ("teacher", 1, "active")],
        )
        self.assertEqual(remaining_records, 0)


if __name__ == "__main__":
    unittest.main()
