import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from src.course_catalog import (
    COURSE_INFOS_URL,
    COURSE_SEARCH_ENDPOINT,
    COURSE_SEARCH_URL,
    DETAIL_REFRESH_AGE,
    refresh_course_catalog,
)
from src.sqlite_store import SQLiteModelStore
from src.upstream_contracts import CATALOG_COURSE_INFOS, CATALOG_COURSE_SEARCH
from src.utils.auth import RequestSession


def summary(code: str, *, valid: bool = True, name: str = "样例") -> dict:
    return {
        "number": code,
        "name": name,
        "ename": "Sample",
        "valid": valid,
        "lastTerm": "2026年春季学期" if valid else None,
        "dept": None,
    }


def profile(code: str, *, valid: bool = True, title: str = "样例") -> dict:
    fields = (
        "courseType dept credit hour grading examType lang textbook ref lastTerm "
        "discipline courseCategory courseClassify sem preq syllabus textbooks desc"
    )
    return {
        **dict.fromkeys(fields.split()),
        "id": 17,
        "code": code,
        "valid": valid,
        "name": {"cn": title, "en": "Sample"},
    }


class CourseCatalogTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_dir = TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.store = SQLiteModelStore(Path(self.temporary_dir.name) / "catalog.sqlite")
        self.addCleanup(self.store.close)
        self.settings = patch.multiple(
            "src.course_catalog",
            DETAIL_BATCH_SIZE=2,
            DETAIL_REQUEST_INTERVAL_SECONDS=0,
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.summaries = [summary("001512"), summary("CA01"), summary("CA02")]
        self.profiles = {
            row["number"]: profile(row["number"]) for row in self.summaries
        }
        self.requests: list[list[str]] = []
        self.keyword_queries: list[str] = []
        self.keyword_rows: dict[str, list[dict]] = {}
        self.next_empty_indexes: list[list[dict]] = []
        self.omit_from_bulk: set[str] = set()
        self.unexpected_code = False
        self.malformed = False
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(self.client.aclose)
        self.session = RequestSession(client=self.client, page=None)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == COURSE_SEARCH_URL:
            self.assertEqual(request.method, "GET")
            self.assertIn(b"keyword=", request.url.query)
            rows = (
                self.next_empty_indexes.pop(0)
                if self.next_empty_indexes
                else self.summaries
            )
            return httpx.Response(200, json=rows)
        if str(request.url).split("?", 1)[0] == COURSE_SEARCH_ENDPOINT:
            self.assertEqual(request.method, "GET")
            keyword = request.url.params["keyword"]
            self.keyword_queries.append(keyword)
            return httpx.Response(200, json=self.keyword_rows.get(keyword, []))

        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), COURSE_INFOS_URL)
        self.assertEqual(request.headers["Content-Type"], "application/json")
        codes = json.loads(request.content)["codes"]
        self.requests.append(codes)
        if self.malformed:
            return httpx.Response(200, json={"error": "upstream unavailable"})
        if self.unexpected_code:
            return httpx.Response(200, json=[profile("UNEXPECTED")])
        return httpx.Response(
            200,
            json=[
                self.profiles[code]
                for code in reversed(codes)
                if code in self.profiles
                and (len(codes) == 1 or code not in self.omit_from_bulk)
            ],
        )

    def details(self) -> dict[str, tuple[str, int]]:
        return {
            code: (payload_json, fetch_id)
            for code, payload_json, fetch_id in self.store.conn.execute(
                f"SELECT code, payload_json, fetch_id FROM {CATALOG_COURSE_INFOS}"
            )
        }

    def metadata(self, key: str) -> str:
        return self.store.conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()[0]

    async def test_batches_exact_codes_and_keeps_distinct_codes_with_same_numeric_id(
        self,
    ) -> None:
        self.summaries.append(dict(self.summaries[0]))
        await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [["001512", "CA01"], ["CA02"]])
        self.assertEqual(list(self.details()), ["001512", "CA01", "CA02"])
        self.assertEqual(self.metadata(f"{CATALOG_COURSE_SEARCH}_count"), "3")
        self.assertEqual(
            json.loads(self.details()["001512"][0]), self.profiles["001512"]
        )

    async def test_refreshes_changed_and_new_codes_and_retains_original_fetches(
        self,
    ) -> None:
        self.profiles["CA01"]["syllabus"] = {
            "template": 1,
            "documents": None,
            "periods": [
                {"experiment": None, "period": 2.5, "nameZh": "旧节次"},
            ],
        }
        await refresh_course_catalog(self.session, self.store)
        original = self.details()
        retained_id = self.store.conn.execute(
            f"SELECT store_id FROM {CATALOG_COURSE_INFOS} WHERE code = ?", ("001512",)
        ).fetchone()[0]
        self.requests.clear()
        self.summaries = [
            summary("001512"),
            summary("CA01", name="新名称"),
            summary("CA03"),
        ]
        self.profiles["CA01"] = profile("CA01", title="新名称")
        self.profiles["CA03"] = profile("CA03")
        await refresh_course_catalog(self.session, self.store)

        latest = self.details()
        self.assertEqual(self.requests, [["CA01", "CA03"]])
        self.assertEqual(set(latest), {"001512", "CA01", "CA03"})
        self.assertEqual(latest["001512"], original["001512"])
        self.assertNotEqual(latest["CA01"], original["CA01"])
        self.assertEqual(
            self.store.conn.execute(
                f"SELECT store_id FROM {CATALOG_COURSE_INFOS} WHERE code = ?",
                ("001512",),
            ).fetchone()[0],
            retained_id,
        )
        self.assertEqual(
            self.store.conn.execute(
                f"SELECT count(*) FROM {CATALOG_COURSE_INFOS}_syllabus_periods"
            ).fetchone()[0],
            0,
        )
        names = self.store.conn.execute(
            f"SELECT p.code, n.cn FROM {CATALOG_COURSE_INFOS} AS p "
            f"JOIN {CATALOG_COURSE_INFOS}_name AS n ON n.parent_store_id = p.store_id"
        ).fetchall()
        self.assertEqual(
            dict(names), {"001512": "样例", "CA01": "新名称", "CA03": "样例"}
        )

        self.requests.clear()
        await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.details(), latest)

    async def test_stale_profiles_refresh_without_an_index_change(self) -> None:
        await refresh_course_catalog(self.session, self.store)
        previous = self.details()
        old = datetime.now(UTC) - DETAIL_REFRESH_AGE - timedelta(days=1)
        self.store.conn.execute(
            "UPDATE upstream_fetches SET fetched_at = ? WHERE id = ?",
            (old.isoformat(), previous["CA02"][1]),
        )
        self.store.conn.commit()
        self.requests.clear()
        self.profiles["CA02"] = profile("CA02", title="仅详情变更")
        await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [["CA02"]])
        self.assertNotEqual(self.details()["CA02"], previous["CA02"])
        self.assertEqual(self.details()["CA01"], previous["CA01"])

    async def test_recovers_sparse_bulk_omission_and_confirms_inactive_without_profile(
        self,
    ) -> None:
        self.summaries[2] = summary("CA02", valid=False)
        del self.profiles["CA02"]
        self.omit_from_bulk.add("CA01")
        await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [["001512", "CA01"], ["CA01"], ["CA02"]])
        self.assertEqual(set(self.details()), {"001512", "CA01"})
        self.assertEqual(self.metadata(f"{CATALOG_COURSE_INFOS}_absent_count"), "1")
        self.assertEqual(
            set(json.loads(self.metadata(f"{CATALOG_COURSE_INFOS}_absent"))),
            {"CA02"},
        )
        self.requests.clear()
        await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [])

    async def test_missing_active_profile_keeps_previous_complete_snapshot(
        self,
    ) -> None:
        await refresh_course_catalog(self.session, self.store)
        original = self.details()
        self.requests.clear()
        self.summaries[1] = summary("CA01", name="改名")
        del self.profiles["CA01"]
        with self.assertRaisesRegex(ValueError, "Active public course 'CA01'"):
            await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [["CA01"]])
        self.assertEqual(self.details(), original)

    async def test_mass_omission_confirms_active_missing_and_keeps_snapshot(
        self,
    ) -> None:
        await refresh_course_catalog(self.session, self.store)
        original = self.details()
        self.requests.clear()
        self.summaries[0] = summary("001512", name="变更")
        self.summaries[1] = summary("CA01", name="变更")
        del self.profiles["001512"]
        self.omit_from_bulk.add("CA01")
        with self.assertRaisesRegex(ValueError, "Active public course '001512'"):
            await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [["001512", "CA01"], ["001512"]])
        self.assertEqual(self.details(), original)

    async def test_wrong_or_malformed_detail_response_stops_refresh(self) -> None:
        for flag, error in (
            ("unexpected_code", "unexpected or repeated code"),
            ("malformed", "validation error"),
        ):
            with self.subTest(flag=flag):
                setattr(self, flag, True)
                with self.assertRaisesRegex(ValueError, error):
                    await refresh_course_catalog(self.session, self.store)
                setattr(self, flag, False)

    async def test_empty_index_is_rejected_even_without_a_cached_snapshot(self) -> None:
        self.summaries.clear()
        with self.assertRaisesRegex(ValueError, "empty index"):
            await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [])

    async def test_conflicting_or_smaller_index_stops_before_any_details(self) -> None:
        await refresh_course_catalog(self.session, self.store)
        original = self.details()
        self.requests.clear()
        removed = self.summaries.pop()
        self.keyword_rows[removed["number"]] = [removed]
        with self.assertRaisesRegex(ValueError, "Keyword search still returns"):
            await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.keyword_queries, [removed["number"]])
        self.summaries.append(summary("001512", name="冲突"))
        with self.assertRaisesRegex(ValueError, "Conflicting public course index"):
            await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.details(), original)

    async def test_consistent_smaller_index_is_saved_after_limited_code_checks(
        self,
    ) -> None:
        extra_codes = [f"OLD{index:02d}" for index in range(20)]
        self.summaries.extend(summary(code) for code in extra_codes)
        self.profiles.update({code: profile(code) for code in extra_codes})
        await refresh_course_catalog(self.session, self.store)
        original = self.details()
        self.requests.clear()
        self.summaries = self.summaries[:3]
        self.keyword_rows["OLD00"] = [summary("OLD00-NEW")]
        self.next_empty_indexes = [self.summaries, self.summaries[::-1]]
        await refresh_course_catalog(self.session, self.store)
        self.assertEqual(
            self.keyword_queries,
            ["OLD00", "OLD02", "OLD05", "OLD08", "OLD10", "OLD13", "OLD16", "OLD19"],
        )
        self.assertEqual(self.next_empty_indexes, [])
        self.assertEqual(self.requests, [])
        self.assertEqual(
            self.details(),
            {row["number"]: original[row["number"]] for row in self.summaries},
        )
        self.assertEqual(self.metadata(f"{CATALOG_COURSE_SEARCH}_count"), "3")

    async def test_recheck_of_smaller_index_must_consistently_omit_same_codes(
        self,
    ) -> None:
        await refresh_course_catalog(self.session, self.store)
        original = self.details()
        self.requests.clear()
        self.next_empty_indexes = [self.summaries[:2], self.summaries[1:]]
        with self.assertRaisesRegex(ValueError, "rechecked public index differs"):
            await refresh_course_catalog(self.session, self.store)
        self.assertEqual(self.keyword_queries, ["CA02"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.details(), original)
