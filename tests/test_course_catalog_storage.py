import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.models.api.catalog_api_teach_course_infos import TeachCourseInfosResponse
from src.models.api.catalog_api_teach_course_search import TeachCourseSearchResponse
from src.sqlite_store import SQLiteModelStore
from src.upstream_contracts import (
    CATALOG_COURSE_INFOS,
    CATALOG_COURSE_SEARCH,
    UPSTREAM_RESPONSE_MODELS,
)

COURSE_CODE = "000001L"
COURSE_URL = "https://catalog.ustc.edu.cn/api/teach/course"


def profile():
    return {
        "id": 123,
        "code": COURSE_CODE,
        "courseType": "理论课",
        "dept": "样例学院",
        "credit": 2.5,
        "hour": 40,
        "grading": "百分制",
        "examType": "论文或报告",
        "lang": "中文",
        "textbook": "资料甲",
        "ref": "参考网页",
        "lastTerm": None,
        "valid": False,
        "discipline": "学科",
        "courseCategory": "一般通识",
        "courseClassify": None,
        "sem": "春;秋",
        "preq": "",
        "syllabus": {
            "template": 41,
            "documents": '{"language-zh":{"paragraphs":[{"content":"<p>实验</p>"}]}}',
            "periods": [
                {"experiment": "实验设计", "period": 1.5, "nameZh": "设计型"},
                {"experiment": "验证", "period": 2, "nameZh": "验证型"},
            ],
        },
        "textbooks": [
            {
                "nameZh": "教材原名",
                "nameEn": None,
                "publish": 1,
                "author": "样例作者",
                "isbn": "00001234",
                "publishingHouse": "样例出版社",
                "edition": "第一版",
                "dates": "2020-10-01 00:00:00",
                "price": 30.25,
                "type": "国（境）内教材",
                "grade": None,
                "content": "<p>说明</p>",
                "remark": None,
            }
        ],
        "name": {"cn": "样例课程", "en": "Course"},
        "desc": {
            "cn": "<p>中文 &amp; <strong>重点</strong></p>",
            "en": "<p>English</p>",
        },
    }


class CourseCatalogStorageTest(unittest.TestCase):
    def register(self, store):
        for name in (CATALOG_COURSE_SEARCH, CATALOG_COURSE_INFOS):
            store.register_response_model(
                table_name=name, response_model=UPSTREAM_RESPONSE_MODELS[name]
            )

    def test_profile_round_trips_relational_fields_and_nested_payload(self):
        item = profile()
        empty = {**item, "id": 124, "code": "001006", "textbooks": [], "syllabus": None}
        unknown = {**empty, "id": 125, "code": "001351", "textbooks": None}
        rows = [item, empty, unknown]
        store = SQLiteModelStore(":memory:")
        self.addCleanup(store.close)
        self.register(store)
        fetch = store.record_fetch(
            source=CATALOG_COURSE_INFOS,
            method="POST",
            url=f"{COURSE_URL}/infos",
            context={"batch": 0},
        )
        count = store.store_response(
            table_name=CATALOG_COURSE_INFOS,
            response=TeachCourseInfosResponse.model_validate(rows),
            fetch_id=fetch,
        )
        self.assertEqual(count, 3)

        records = store.conn.execute(
            """
            SELECT id, code, credit, valid, position, payload_json
            FROM catalog_teach_course_infos
            WHERE fetch_id = ?
            ORDER BY position
            """,
            (fetch,),
        ).fetchall()
        self.assertEqual(records[0][:-1], (123, COURSE_CODE, 2.5, 0, 0))
        self.assertEqual([json.loads(row[-1]) for row in records], rows)
        texts = store.conn.execute(
            """
            SELECT desc.cn, desc.en, syllabus.documents
            FROM catalog_teach_course_infos AS info
            JOIN catalog_teach_course_infos_desc AS desc
              ON desc.parent_store_id = info.store_id
            JOIN catalog_teach_course_infos_syllabus AS syllabus
              ON syllabus.parent_store_id = info.store_id
            WHERE info.fetch_id = ?
            """,
            (fetch,),
        ).fetchone()
        self.assertEqual(
            texts,
            (item["desc"]["cn"], item["desc"]["en"], item["syllabus"]["documents"]),
        )
        textbooks = store.conn.execute(
            """
            SELECT isbn, publishingHouse, content
            FROM catalog_teach_course_infos_textbooks
            WHERE fetch_id = ?
            """,
            (fetch,),
        ).fetchall()
        self.assertEqual(textbooks, [("00001234", "样例出版社", "<p>说明</p>")])
        periods = store.conn.execute(
            """
            SELECT period, nameZh
            FROM catalog_teach_course_infos_syllabus_periods
            WHERE fetch_id = ?
            ORDER BY position
            """,
            (fetch,),
        ).fetchall()
        self.assertEqual(periods, [(1.5, "设计型"), (2, "验证型")])

        store.delete_fetches([fetch])
        for table in (
            "catalog_teach_course_infos",
            "catalog_teach_course_infos_desc",
            "catalog_teach_course_infos_syllabus",
            "catalog_teach_course_infos_syllabus_periods",
            "catalog_teach_course_infos_textbooks",
        ):
            with self.subTest(table=table):
                rows_left = store.conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()
                self.assertEqual(rows_left, (0,))

    def test_search_registration_is_additive_and_keeps_fetch_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            old = SQLiteModelStore(path)
            fetch_id = old.record_fetch(
                source="existing-fixture", method="GET", url="https://example.test"
            )
            old.close()

            store = SQLiteModelStore(path, reset=False)
            try:
                self.register(store)
                item = {
                    "number": COURSE_CODE,
                    "name": "样例课程",
                    "ename": None,
                    "valid": False,
                    "lastTerm": None,
                    "dept": "样例学院",
                }
                fetch = store.record_fetch(
                    source=CATALOG_COURSE_SEARCH,
                    method="GET",
                    url=f"{COURSE_URL}/search",
                )
                store.store_response(
                    table_name=CATALOG_COURSE_SEARCH,
                    response=TeachCourseSearchResponse.model_validate([item]),
                    fetch_id=fetch,
                )
                number, payload, fetched_at = store.conn.execute(
                    """
                    SELECT search.number, search.payload_json, fetch.fetched_at
                    FROM catalog_teach_course_search AS search
                    JOIN upstream_fetches AS fetch ON fetch.id = search.fetch_id
                    WHERE fetch.source = ?
                    """,
                    (CATALOG_COURSE_SEARCH,),
                ).fetchone()
                self.assertEqual(number, COURSE_CODE)
                self.assertEqual(json.loads(payload), item)
                self.assertIsNotNone(fetched_at)
                self.assertEqual(
                    store.conn.execute(
                        "SELECT source FROM upstream_fetches WHERE id = ?", (fetch_id,)
                    ).fetchone(),
                    ("existing-fixture",),
                )
            finally:
                store.close()

    def test_malformed_profile_is_rejected_without_coercing_source_identity(self):
        item = profile()
        for invalid in (
            {**item, "code": 1},
            {**item, "id": "123"},
            {**item, "valid": 1},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                TeachCourseInfosResponse.model_validate([invalid])
        self.assertEqual(TeachCourseInfosResponse.model_validate([]).root, [])
