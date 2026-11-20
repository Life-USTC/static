import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from src.course_catalog import COURSE_SEARCH_URL, refresh_course_catalog
from src.models.api.catalog_api_teach_course_infos import TeachCourseInfo
from src.sqlite_store import SQLiteModelStore
from src.upstream_contracts import CATALOG_COURSE_INFOS as TABLE


def summary(code, name="样例", valid=True):
    return dict(
        number=code, name=name, ename=None, valid=valid, lastTerm=None, dept=None
    )


def profile(code):
    fields = TeachCourseInfo.model_fields
    return dict.fromkeys(field.alias or name for name, field in fields.items()) | dict(
        id=17, code=code, valid=True, name={"cn": "样例", "en": None}
    )


class CourseCatalogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SQLiteModelStore(":memory:")
        self.addCleanup(self.store.close)
        self.db = self.store.conn
        self.enterContext(
            patch.multiple(
                "src.course_catalog",
                DETAIL_BATCH_SIZE=2,
                DETAIL_REQUEST_INTERVAL_SECONDS=0,
            )
        )
        self.index = [summary(code) for code in ("001512", "B", "C")]
        self.profiles = {row["number"]: profile(row["number"]) for row in self.index}
        self.rest, self.probes, self.requests = {}, [], []
        self.index_reads = []
        self.omit = set()
        self.override = None

    async def get(self, url, **_):
        if url == COURSE_SEARCH_URL:
            return self.index_reads.pop(0) if self.index_reads else self.index
        (code,) = parse_qs(urlsplit(url).query)["keyword"]
        self.probes.append(code)
        return self.rest.get(code, [])

    async def post(self, url, data, **_):
        codes = data["codes"]
        self.requests.append(codes)
        if self.override is not None:
            return self.override
        return [
            self.profiles[code]
            for code in reversed(codes)
            if code in self.profiles and (len(codes) == 1 or code not in self.omit)
        ]

    async def refresh(self):
        self.requests.clear()
        await refresh_course_catalog(
            SimpleNamespace(get_json=self.get, post_json=self.post), self.store
        )

    def saved(self):
        return dict(self.db.execute(f"SELECT code, payload_json FROM {TABLE}"))

    async def test_lossless_profiles_incremental_refresh_and_descendant_cleanup(self):
        syllabus = {
            "template": 1,
            "documents": '{"body":"<p>实验</p>"}',
            "periods": [
                {"experiment": "实验", "period": 1.5, "nameZh": "设计型"},
            ],
        }
        self.profiles["B"].update(credit=2.5, syllabus=syllabus, textbooks=[])
        self.index.append(self.index[0])
        await self.refresh()
        self.assertEqual(
            {code: json.loads(raw) for code, raw in self.saved().items()}, self.profiles
        )
        fields = self.db.execute(f"SELECT period FROM {TABLE}_syllabus_periods")
        self.assertEqual(fields.fetchall(), [(1.5,)])
        old = datetime.now(UTC) - timedelta(days=15)
        self.db.execute(
            f"UPDATE upstream_fetches SET fetched_at=? WHERE id IN "
            f"(SELECT fetch_id FROM {TABLE} WHERE code='C')",
            (old.isoformat(),),
        )
        self.profiles["C"]["name"]["cn"] = "更新详情"
        await self.refresh()
        self.assertEqual(self.requests, [["C"]])
        self.index[1], self.profiles["B"] = summary("B", "更新"), profile("B")
        await self.refresh()
        self.assertEqual(self.requests, [["B"]])
        self.assertFalse(
            self.db.execute(f"SELECT * FROM {TABLE}_syllabus_periods").fetchall()
        )
        await self.refresh()
        self.assertEqual(self.requests, [])

    async def test_confirmed_inactive_omission_and_invalid_responses(self):
        self.index[2] = summary("C", valid=False)
        del self.profiles["C"]
        self.omit.add("B")
        await self.refresh()
        self.assertEqual(set(self.saved()), {"001512", "B"})
        original = self.saved()
        await self.refresh()
        self.assertEqual(self.requests, [])
        self.index[0] = summary("001512", "更新")
        del self.profiles["001512"]
        cases = {
            "Active public course": None,
            "unexpected": [profile("UNKNOWN")],
            "validation error": {"invalid": True},
        }
        for message, response in cases.items():
            self.override = response
            with self.assertRaisesRegex(ValueError, message):
                await self.refresh()
            self.assertEqual(self.saved(), original)

    async def test_smaller_index_requires_corroboration_and_bounded_probes(self):
        base = self.index.copy()
        extras = [f"OLD{i:02d}" for i in range(20)]
        self.index.extend(map(summary, extras))
        self.profiles.update((code, profile(code)) for code in extras)
        await self.refresh()
        original = self.saved()
        for index, message in (
            ([], "empty index"),
            (base + [summary("C", "冲突")], "Conflicting"),
        ):
            self.index = index
            with self.assertRaisesRegex(ValueError, message):
                await self.refresh()
            self.assertEqual(self.saved(), original)
        self.index = base
        self.rest[extras[0]] = [summary(extras[0])]
        with self.assertRaisesRegex(ValueError, "Keyword search still returns"):
            await self.refresh()
        self.rest.clear()
        self.index_reads = [base, base[:-1]]
        with self.assertRaisesRegex(ValueError, "rechecked public index differs"):
            await self.refresh()
        self.probes.clear()
        await self.refresh()
        self.assertEqual((len(self.probes), self.requests), (8, []))
        self.assertEqual(set(self.saved()), {"001512", "B", "C"})
