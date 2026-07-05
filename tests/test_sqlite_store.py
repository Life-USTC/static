import tempfile
import unittest
from pathlib import Path

from src.sqlite_store import SQLiteModelStore


class SQLiteModelStoreCacheTest(unittest.TestCase):
    def test_can_reopen_existing_store_without_resetting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.sqlite"
            store = SQLiteModelStore(path)
            store.record_fetch(source="source", method="GET", url="https://example.test")
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


if __name__ == "__main__":
    unittest.main()
