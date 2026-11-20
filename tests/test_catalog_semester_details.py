import asyncio
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from src.catalog_semester_details import (
    CATALOG_LESSON_INFOS,
    CATALOG_PUBLIC_LESSONS,
    CATALOG_PUBLIC_SEMESTERS,
    LESSON_INFOS_URL,
    LESSON_LIST_URL,
    SEMESTER_LIST_URL,
    TeachLessonInfo,
    _profile_batches,
    refresh_catalog_semester_details,
)
from src.models.api.catalog_api_teach_lesson_list_for_teach import (
    TeachLessonListItem,
    TeachLessonListResponse,
)
from src.sqlite_store import SQLiteModelStore
from src.upstream_contracts import CATALOG_LESSONS
from src.utils.auth import RequestSession

ACTIVE = 461
HISTORY = 221
FUTURE = 481
NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def semester(id_: int, start: str, end: str) -> dict:
    return {
        "id": id_,
        "nameZh": "样例学期",
        "code": str(id_),
        "start": start,
        "end": end,
        "isLast": id_ == ACTIVE,
    }


def lesson(id_: int, code: str) -> dict:
    return {
        **dict.fromkeys(TeachLessonListItem.model_fields),
        "id": id_,
        "code": code,
        "course": {
            "id": id_ + 100,
            "code": code.split(".")[0],
            "cn": "目录",
            "en": None,
        },
    }


def profile(row: dict) -> dict:
    data = {
        **dict.fromkeys(
            field.alias or name for name, field in TeachLessonInfo.model_fields.items()
        ),
        "id": row["id"],
        "code": row["code"],
        "courseId": row["course"]["id"],
        "valid": True,
        "name": {"cn": "教学班", "en": None},
        "preq": "",
        "textbooks": [],
    }
    if row["id"] % 2:
        data.pop("lessonLang")
    return data


class CatalogSemesterDetailsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = SQLiteModelStore(":memory:")
        self.addCleanup(self.store.close)
        self.terms = [
            semester(HISTORY, "2021-09-01", "2022-01-21"),
            semester(ACTIVE, "2026-08-30", "2027-01-15"),
            semester(FUTURE, "2027-02-21", "2027-06-25"),
        ]
        self.active_index = [lesson(20, "MA01.01"), lesson(21, "MA01.02")]
        self.history_index = [lesson(5, "MA01.01"), lesson(6, "OLD01.01")]
        self.post_requests: list[tuple[int, list[str]]] = []
        self.get_requests: list[str] = []
        self.missing: set[str] = set()
        self.temporary_omit: set[str] = set()
        self.wrong_id = False
        self.history_status = 200
        self.history_index_status = 200
        self.excluded_profiles: list[dict] = []
        clock = patch("src.catalog_semester_details.datetime", wraps=datetime)
        clock.start().now.return_value = NOW
        self.addCleanup(clock.stop)
        cadence = patch.multiple(
            "src.catalog_semester_details",
            REQUEST_INTERVAL_SECONDS=0,
            BATCH_SIZE=2,
            MISSING_PROBE_LIMIT=1,
        )
        cadence.start()
        self.addCleanup(cadence.stop)
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(client.aclose)
        self.session = RequestSession(client=client, page=None)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET":
            self.get_requests.append(url)
            if url == SEMESTER_LIST_URL:
                return httpx.Response(200, json=self.terms)
            if url == f"{LESSON_LIST_URL}/{HISTORY}":
                return httpx.Response(
                    self.history_index_status, json=self.history_index
                )
            self.assertEqual(url, f"{LESSON_LIST_URL}/{ACTIVE}")
            return httpx.Response(200, json=self.active_index)

        self.assertEqual((request.method, url), ("POST", LESSON_INFOS_URL))
        request_body = json.loads(request.content)
        term, codes = request_body["semester"], request_body["codes"]
        self.post_requests.append((term, codes))
        rows = (
            self.active_index + self.excluded_profiles
            if term == ACTIVE
            else self.history_index
        )
        if term == HISTORY and self.history_status != 200:
            return httpx.Response(self.history_status)
        result = [
            profile(row)
            for row in reversed(rows)
            if row["code"] in codes
            and row["code"] not in self.missing
            and (len(codes) == 1 or row["code"] not in self.temporary_omit)
        ]
        if self.wrong_id and result:
            result[-1]["id"] += 1
        return httpx.Response(200, json=result)

    def seed_known_history(self, *, verified: bool = False) -> None:
        with self.store.conn:
            fetch_id = self.store.record_fetch(
                source=CATALOG_LESSONS,
                method="GET",
                url=f"{LESSON_LIST_URL}/{HISTORY}",
                context={"semester_id": str(HISTORY)},
            )
            self.store.store_response(
                table_name=CATALOG_LESSONS,
                response=TeachLessonListResponse.model_validate(self.history_index),
                fetch_id=fetch_id,
                context={"semester_id": str(HISTORY)},
            )
            if verified:
                self.store.put_metadata(
                    {
                        "curriculum_fetch_status": "complete",
                        "curriculum_successful_semester_ids": str(HISTORY),
                        "curriculum_unavailable_semester_ids": "",
                    }
                )

    def rows(self, table: str) -> list[tuple]:
        return self.store.conn.execute(
            f"SELECT id, code, semester_id, fetch_id FROM {table} ORDER BY id"
        ).fetchall()

    def metadata(self, term: int) -> dict:
        return json.loads(
            self.store.conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (f"catalog_semester_details_{term}",),
            ).fetchone()[0]
        )

    async def test_parallel_batches_remain_bounded_and_preserve_order(self) -> None:
        in_flight = 0
        peak = 0
        codes = [str(i) for i in range(12)]

        async def fetch(session, semester_id, batch, lessons):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.003 if batch[0] == "2" else 0.001)
            in_flight -= 1
            return {code: code for code in batch}

        with patch("src.catalog_semester_details._profile_batch", fetch):
            results = [
                code
                async for _, records in _profile_batches(
                    self.session, ACTIVE, codes, {}, False
                )
                for code in records
            ]
        self.assertEqual(results, codes)
        self.assertEqual(peak, 3)

    async def test_clean_database_stores_active_source_and_profiles(self) -> None:
        self.active_index.append(lesson(22, "PH01.01"))
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(
            self.get_requests, [SEMESTER_LIST_URL, f"{LESSON_LIST_URL}/{ACTIVE}"]
        )
        self.assertEqual(
            self.post_requests,
            [(ACTIVE, ["MA01.01", "MA01.02"]), (ACTIVE, ["PH01.01"])],
        )
        self.assertEqual(
            self.store.conn.execute(
                f"SELECT id FROM {CATALOG_PUBLIC_SEMESTERS} ORDER BY id"
            ).fetchall(),
            [(HISTORY,), (ACTIVE,), (FUTURE,)],
        )
        self.assertEqual(
            [row[:3] for row in self.rows(CATALOG_LESSON_INFOS)],
            [(20, "MA01.01", "461"), (21, "MA01.02", "461"), (22, "PH01.01", "461")],
        )
        self.assertEqual(
            [
                json.loads(payload)
                for (payload,) in self.store.conn.execute(
                    f"SELECT payload_json FROM {CATALOG_LESSON_INFOS} ORDER BY id"
                )
            ],
            [profile(row) for row in self.active_index],
        )
        self.assertEqual(self.metadata(ACTIVE)["unavailable_lesson_codes"], [])
        fetch_id = self.rows(CATALOG_LESSON_INFOS)[0][-1]
        self.assertEqual(
            self.store.conn.execute(
                "SELECT method, url, context FROM upstream_fetches WHERE id = ?",
                (fetch_id,),
            ).fetchone(),
            ("POST", LESSON_INFOS_URL, f"semester_id={ACTIVE}"),
        )
        self.post_requests.clear()
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(self.post_requests, [])
        self.assertEqual(self.rows(CATALOG_LESSON_INFOS)[0][-1], fetch_id)

    async def test_still_crawls_last_published_semester_in_a_gap(self) -> None:
        active = next(term for term in self.terms if term["id"] == ACTIVE)
        active["end"] = "2026-11-19"
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertIn(f"{LESSON_LIST_URL}/{ACTIVE}", self.get_requests)
        self.assertEqual(self.metadata(ACTIVE)["lesson_profile_rows"], 2)

    async def test_backfills_available_history_and_never_mutates_legacy_source(self):
        self.seed_known_history()
        old_legacy = self.rows(CATALOG_LESSONS)
        await refresh_catalog_semester_details(self.session, self.store)
        rows = self.rows(CATALOG_LESSON_INFOS)
        self.assertEqual([row[2] for row in rows], ["221", "221", "461", "461"])
        self.assertEqual(self.rows(CATALOG_LESSONS), old_legacy)
        self.assertEqual(self.metadata(HISTORY)["lesson_profile_rows"], 2)
        self.assertIn(f"{LESSON_LIST_URL}/{HISTORY}", self.get_requests)
        self.post_requests.clear()
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(self.post_requests, [])
        self.assertEqual(self.rows(CATALOG_LESSON_INFOS), rows)

    async def test_corroborated_settled_curriculum_does_not_duplicate_the_index(self):
        self.seed_known_history(verified=True)
        old_legacy = self.rows(CATALOG_LESSONS)
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(
            self.get_requests, [SEMESTER_LIST_URL, f"{LESSON_LIST_URL}/{ACTIVE}"]
        )
        self.assertIn((HISTORY, ["MA01.01", "OLD01.01"]), self.post_requests)
        self.assertEqual(self.rows(CATALOG_LESSONS), old_legacy)
        self.assertFalse(
            any(row[2] == str(HISTORY) for row in self.rows(CATALOG_PUBLIC_LESSONS))
        )
        self.assertEqual(self.metadata(HISTORY)["lesson_index_source"], "curriculum")
        self.assertEqual(self.metadata(HISTORY)["lesson_profile_rows"], 2)
        self.assertEqual(
            self.store.conn.execute(
                "SELECT source, method, ok FROM upstream_fetches "
                "WHERE context = ? ORDER BY id",
                (f"semester_id={HISTORY}",),
            ).fetchall(),
            [(CATALOG_LESSONS, "GET", 1), (CATALOG_LESSON_INFOS, "POST", 1)],
        )
        self.post_requests.clear()
        self.get_requests.clear()
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertNotIn(f"{LESSON_LIST_URL}/{HISTORY}", self.get_requests)
        self.assertEqual(self.post_requests, [])

    async def test_omitted_curriculum_code_retries_the_live_index(self):
        self.seed_known_history(verified=True)
        self.history_index[-1] = lesson(7, "NEW01.01")
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertIn(f"{LESSON_LIST_URL}/{HISTORY}", self.get_requests)
        self.assertEqual(
            [codes for term, codes in self.post_requests if term == HISTORY],
            [["MA01.01", "OLD01.01"], ["MA01.01", "NEW01.01"]],
        )
        self.assertEqual(self.metadata(HISTORY)["lesson_index_source"], "public")
        self.assertEqual(
            [
                row[1]
                for row in self.rows(CATALOG_LESSON_INFOS)
                if row[2] == str(HISTORY)
            ],
            ["MA01.01", "NEW01.01"],
        )

    async def test_conflicting_curriculum_id_retries_the_live_index(self):
        self.seed_known_history(verified=True)
        self.history_index[-1] = lesson(7, "OLD01.01")
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertIn(f"{LESSON_LIST_URL}/{HISTORY}", self.get_requests)
        self.assertEqual(self.metadata(HISTORY)["lesson_index_source"], "public")
        self.assertEqual(
            [
                row[0]
                for row in self.rows(CATALOG_LESSON_INFOS)
                if row[2] == str(HISTORY)
            ],
            [5, 7],
        )

    async def test_success_metadata_without_a_successful_fetch_uses_live_index(self):
        self.seed_known_history(verified=True)
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE upstream_fetches SET ok = 0 WHERE source = ? AND context = ?",
                (CATALOG_LESSONS, f"semester_id={HISTORY}"),
            )
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertIn(f"{LESSON_LIST_URL}/{HISTORY}", self.get_requests)
        self.assertEqual(self.metadata(HISTORY)["lesson_index_source"], "public")

    async def test_recent_and_explicit_terms_still_check_live_index(self):
        self.seed_known_history(verified=True)
        history = next(term for term in self.terms if term["id"] == HISTORY)
        history["end"] = "2026-10-01"
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertIn(f"{LESSON_LIST_URL}/{HISTORY}", self.get_requests)
        self.assertEqual(self.metadata(HISTORY)["lesson_index_source"], "public")

        self.get_requests.clear()
        self.post_requests.clear()
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertIn(f"{LESSON_LIST_URL}/{HISTORY}", self.get_requests)
        self.assertEqual(self.post_requests, [])

        history["end"] = "2022-01-21"
        self.get_requests.clear()
        await refresh_catalog_semester_details(
            self.session, self.store, semester_ids=[HISTORY]
        )
        self.assertEqual(
            self.get_requests, [SEMESTER_LIST_URL, f"{LESSON_LIST_URL}/{HISTORY}"]
        )

    async def test_missing_codes_are_retried_and_reconciled(self) -> None:
        self.temporary_omit.add("MA01.02")
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(
            self.post_requests,
            [(ACTIVE, ["MA01.01", "MA01.02"]), (ACTIVE, ["MA01.02"])],
        )
        self.assertEqual(self.metadata(ACTIVE)["unavailable_lesson_codes"], [])
        self.active_index.append(lesson(22, "PH01.01"))
        self.missing.add("MA01.02")
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(self.metadata(ACTIVE)["unavailable_lesson_codes"], ["MA01.02"])
        self.assertEqual(self.metadata(ACTIVE)["lesson_count"], 3)
        self.assertEqual(self.metadata(ACTIVE)["lesson_profile_rows"], 2)

    async def test_partial_new_snapshot_and_wrong_id_preserve_old_atomically(self):
        await refresh_catalog_semester_details(self.session, self.store)
        original = self.rows(CATALOG_PUBLIC_LESSONS), self.rows(CATALOG_LESSON_INFOS)
        metadata = self.metadata(ACTIVE)
        self.active_index.append(lesson(22, "PH01.01"))
        self.missing.update(("MA01.01", "MA01.02"))
        self.post_requests.clear()
        with self.assertRaisesRegex(RuntimeError, "461"):
            await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(len(self.post_requests), 2)
        self.assertEqual(
            (self.rows(CATALOG_PUBLIC_LESSONS), self.rows(CATALOG_LESSON_INFOS)),
            original,
        )
        self.assertEqual(self.metadata(ACTIVE), metadata)

        self.missing.clear()
        self.wrong_id = True
        with self.assertRaisesRegex(RuntimeError, "461"):
            await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(
            (self.rows(CATALOG_PUBLIC_LESSONS), self.rows(CATALOG_LESSON_INFOS)),
            original,
        )
        self.assertEqual(self.metadata(ACTIVE), metadata)

    async def test_historic_404_is_recorded_without_erasing_public_active_rows(self):
        self.seed_known_history(verified=True)
        self.history_status = 404
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(
            [row[2] for row in self.rows(CATALOG_LESSON_INFOS)], ["461", "461"]
        )
        history_requests = [
            codes for term, codes in self.post_requests if term == HISTORY
        ]
        self.assertEqual(
            history_requests,
            [["MA01.01", "OLD01.01"], ["MA01.01", "OLD01.01"], ["MA01.01"]],
        )
        self.assertIn(f"{LESSON_LIST_URL}/{HISTORY}", self.get_requests)
        self.assertTrue(self.metadata(HISTORY)["lesson_infos_unavailable"])
        self.assertEqual(
            self.store.conn.execute(
                "SELECT ok FROM upstream_fetches WHERE source = ? AND context = ?",
                (CATALOG_LESSON_INFOS, f"semester_id={HISTORY}"),
            ).fetchone(),
            (0,),
        )
        self.post_requests.clear()
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(self.post_requests, [])

    async def test_selects_explicit_historical_semester_on_clean_database(self):
        await refresh_catalog_semester_details(
            self.session, self.store, semester_ids=[HISTORY]
        )
        self.assertEqual(
            self.get_requests, [SEMESTER_LIST_URL, f"{LESSON_LIST_URL}/{HISTORY}"]
        )
        self.assertEqual(self.post_requests, [(HISTORY, ["MA01.01", "OLD01.01"])])
        self.assertEqual(
            [row[2] for row in self.rows(CATALOG_LESSON_INFOS)], ["221", "221"]
        )
        self.assertEqual(self.metadata(HISTORY)["lesson_index_source"], "public")
        indexes = {
            row[1]: row[2]
            for row in self.store.conn.execute(
                f"PRAGMA index_list({CATALOG_LESSON_INFOS})"
            )
        }
        self.assertEqual(indexes["catalog_lesson_infos_semester_code"], 1)
        self.assertEqual(indexes["catalog_lesson_infos_course_semester"], 0)
        self.post_requests.clear()
        await refresh_catalog_semester_details(
            self.session, self.store, semester_ids=[HISTORY]
        )
        self.assertEqual(self.post_requests, [])

    async def test_requested_historical_list_missing_is_visible(self):
        self.history_index_status = 404
        with self.assertRaisesRegex(RuntimeError, "221"):
            await refresh_catalog_semester_details(
                self.session, self.store, semester_ids=[HISTORY]
            )
        self.assertEqual(
            self.get_requests,
            [SEMESTER_LIST_URL, f"{LESSON_LIST_URL}/{HISTORY}"]
            + [f"{LESSON_LIST_URL}/{HISTORY}"],
        )
        self.assertEqual(self.post_requests, [])
        self.assertEqual(self.metadata(HISTORY)["lesson_index_source"], "unavailable")
        self.assertTrue(self.metadata(HISTORY)["lesson_infos_unavailable"])
        self.assertEqual(
            self.store.conn.execute(
                "SELECT ok FROM upstream_fetches WHERE source = ? AND context = ?",
                (CATALOG_PUBLIC_LESSONS, f"semester_id={HISTORY}"),
            ).fetchone(),
            (0,),
        )

    async def test_default_history_refresh_labels_cached_index_fallback(self):
        self.seed_known_history()
        self.history_index_status = 410
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(self.metadata(HISTORY)["lesson_index_source"], "cached")
        self.assertEqual(self.metadata(HISTORY)["lesson_profile_rows"], 2)
        self.assertIn((HISTORY, ["MA01.01", "OLD01.01"]), self.post_requests)

    async def test_corroborates_shrink_and_preserves_previously_verified_snapshot(self):
        await refresh_catalog_semester_details(self.session, self.store)
        old_ids = self.rows(CATALOG_LESSON_INFOS)
        removed = self.active_index.pop()
        self.excluded_profiles = [removed]
        self.get_requests.clear()
        self.post_requests.clear()
        with self.assertRaisesRegex(RuntimeError, "461"):
            await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(self.rows(CATALOG_LESSON_INFOS), old_ids)
        self.assertEqual(self.get_requests.count(f"{LESSON_LIST_URL}/{ACTIVE}"), 2)
        self.assertEqual(self.post_requests, [(ACTIVE, ["MA01.02"])])

        self.excluded_profiles.clear()
        self.get_requests.clear()
        self.post_requests.clear()
        await refresh_catalog_semester_details(self.session, self.store)
        self.assertEqual(
            [row[1] for row in self.rows(CATALOG_LESSON_INFOS)], ["MA01.01"]
        )
        self.assertEqual(self.get_requests.count(f"{LESSON_LIST_URL}/{ACTIVE}"), 2)
        self.assertEqual(
            self.post_requests, [(ACTIVE, ["MA01.02"]), (ACTIVE, ["MA01.01"])]
        )
