from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from src.blackboard import (
    BlackboardConfig,
    CourseConfig,
    GuestSession,
    crawl_course,
    guest_login_url,
    load_blackboard_config,
    load_previous_resources,
    make_blackboard,
)
from src.models.blackboard import (
    ACCESS_AUTH_REQUIRED,
    ACCESS_MODE_GUEST,
    ACCESS_PUBLIC,
    CATEGORY_HOMEWORK,
    CATEGORY_LAB,
    CATEGORY_OTHER,
    CATEGORY_REFERENCE,
    CATEGORY_SLIDES,
    PAGE_KIND_CONTENT,
    PAGE_KIND_LOGIN,
    PAGE_KIND_NAVIGATION,
    PAGE_KIND_NOT_FOUND,
)
from src.sqlite_store import SNAPSHOT_FILENAME, SQLiteModelStore
from src.utils.blackboard_html import (
    classify_page,
    classify_resource,
    filename_from_url,
    is_login_document,
    is_not_found_document,
    parse_content_items,
    parse_course_menu,
    parse_course_title,
)

FIXTURES = Path(__file__).parent / "fixtures" / "blackboard"
COURSE_ID = "_297_1"
BASE_URL = "https://www.bb.ustc.edu.cn"

PDF_BODY = b"%PDF-1.4 recorded fixture body\n"
PDF_SHA256 = hashlib.sha256(PDF_BODY).hexdigest()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class BlackboardParsingTest(unittest.TestCase):
    def test_course_menu_lists_content_areas_in_order(self) -> None:
        areas = parse_course_menu(fixture("course_menu.html"), course_id=COURSE_ID)

        self.assertEqual(
            [area.content_id for area in areas],
            ["_12953_1", "_12954_1", "_12955_1", "_12956_1"],
        )
        self.assertEqual(areas[0].title, "Course Materials:课程PPT")

    def test_course_title_is_taken_from_the_page_title(self) -> None:
        self.assertEqual(
            parse_course_title(fixture("course_menu.html")),
            "GPU并行计算和CUDA程序开发及优化",
        )

    def test_content_items_expose_files_folders_and_attachments(self) -> None:
        items = parse_content_items(
            fixture("content_listing.html"), course_id=COURSE_ID
        )

        self.assertEqual(
            [(item.item_id, item.kind) for item in items],
            [
                ("_12958_1", "file"),
                ("_12961_1", "file"),
                ("_12962_1", "folder"),
                ("_12968_1", "file"),
            ],
        )
        self.assertEqual(items[0].title, "Lecture_Intro_2014_Autumn.pdf")
        self.assertEqual(
            items[0].resources[0].url,
            "/bbcswebdav/pid-12958-dt-content-rid-50834_1/xid-50834_1",
        )
        self.assertEqual(items[2].folder_content_id, "_12962_1")
        self.assertEqual(items[2].description, "第一次实验材料")
        self.assertEqual(
            items[3].resources[0].url,
            "/bbcswebdav/pid-12968-dt-content-rid-50851_1/xid-50851_1",
        )

    def test_localisation_bundle_is_not_mistaken_for_a_login_form(self) -> None:
        # Every Blackboard page ships a JS bundle that mentions "password".
        self.assertFalse(is_login_document(fixture("content_listing.html")))
        self.assertTrue(is_login_document(fixture("login_shell.html")))

    def test_resource_not_found_placeholder_is_recognised(self) -> None:
        self.assertTrue(is_not_found_document(fixture("not_found.html")))
        self.assertFalse(is_not_found_document(fixture("content_listing.html")))

    def test_page_classification_separates_content_from_noise(self) -> None:
        listing = fixture("content_listing.html")
        self.assertEqual(
            classify_page(
                status=200, final_url=f"{BASE_URL}/c", html=listing, item_count=4
            ),
            PAGE_KIND_CONTENT,
        )
        self.assertEqual(
            classify_page(
                status=200,
                final_url=f"{BASE_URL}/c",
                html=fixture("empty_listing.html"),
                item_count=0,
            ),
            PAGE_KIND_NAVIGATION,
        )
        self.assertEqual(
            classify_page(
                status=200,
                final_url=f"{BASE_URL}/c",
                html=fixture("login_shell.html"),
                item_count=0,
            ),
            PAGE_KIND_LOGIN,
        )
        self.assertEqual(
            classify_page(
                status=200,
                final_url=f"{BASE_URL}/c",
                html=fixture("not_found.html"),
                item_count=0,
            ),
            PAGE_KIND_NOT_FOUND,
        )
        self.assertEqual(
            classify_page(
                status=403, final_url=f"{BASE_URL}/c", html=listing, item_count=4
            ),
            PAGE_KIND_LOGIN,
        )

    def test_resources_are_categorised_from_title_and_filename(self) -> None:
        self.assertEqual(classify_resource("第二次作业参考材料"), CATEGORY_HOMEWORK)
        self.assertEqual(
            classify_resource("第一次实验", "Week2-Labwork-1.ppt"), CATEGORY_LAB
        )
        self.assertEqual(
            classify_resource("Course PPT", "Week3-CUDA.pdf"), CATEGORY_SLIDES
        )
        self.assertEqual(classify_resource("References:课程资料"), CATEGORY_REFERENCE)
        self.assertEqual(classify_resource("FinalProject"), CATEGORY_OTHER)

    def test_filename_comes_from_the_resolved_url(self) -> None:
        self.assertEqual(
            filename_from_url(
                f"{BASE_URL}/bbcswebdav/pid-12961-dt-content-rid-50846_1"
                "/courses/ESB5316/Week2-Labwork-1.ppt"
            ),
            "Week2-Labwork-1.ppt",
        )


class GuestLoginUrlTest(unittest.TestCase):
    def test_guest_login_url_matches_the_documented_endpoint(self) -> None:
        self.assertEqual(
            guest_login_url(BASE_URL, COURSE_ID),
            "https://www.bb.ustc.edu.cn/webapps/login?action=guest_login&new_loc"
            "=%2Fwebapps%2Fblackboard%2Fexecute%2Flauncher%3Ftype%3DCourse%26id"
            "%3D_297_1",
        )

    def test_guest_login_url_carries_no_credentials(self) -> None:
        query = parse_qs(urlsplit(guest_login_url(BASE_URL, COURSE_ID)).query)

        self.assertEqual(query["action"], ["guest_login"])
        for credential_field in ("user_id", "password", "encoded_pw", "one_time_token"):
            self.assertNotIn(credential_field, query)


class RecordingTransport(httpx.MockTransport):
    """A fixture-backed Blackboard that records every request it is sent."""

    def __init__(self, *, resource_status: dict[str, int] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.resource_status = resource_status or {}
        self.conditional_headers: list[dict[str, str]] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        query = parse_qs(request.url.query.decode())

        if path.startswith("/webapps/login"):
            return httpx.Response(
                200,
                html="<html><body>guest session established</body></html>",
                headers={"set-cookie": "session_id=guest-session; Path=/"},
            )
        if path == "/webapps/blackboard/execute/launcher":
            return httpx.Response(200, html=fixture("course_menu.html"))
        if path == "/webapps/blackboard/content/listContent.jsp":
            content_id = query.get("content_id", [""])[0]
            bodies = {
                "_12953_1": "content_listing.html",
                # The same body under a second content id: a duplicate page.
                "_12954_1": "content_listing.html",
                "_12955_1": "empty_listing.html",
                "_12956_1": "not_found.html",
                "_12962_1": "empty_listing.html",
            }
            name = bodies.get(content_id)
            if name is None:
                return httpx.Response(200, html=fixture("not_found.html"))
            return httpx.Response(200, html=fixture(name))
        if path.startswith("/bbcswebdav/") and "/xid-" in path:
            self.conditional_headers.append(
                {
                    key: value
                    for key, value in request.headers.items()
                    if key in {"if-none-match", "if-modified-since"}
                }
            )
            status = self.resource_status.get(path, 200)
            if status != 200:
                return httpx.Response(status, html=fixture("login_shell.html"))
            if request.headers.get("if-none-match"):
                # Blackboard answers a revalidation with an empty 304.
                return httpx.Response(304)
            pid = path.split("/")[2]
            resolved = f"/bbcswebdav/{pid}/courses/ESB5316/{pid}.pdf"
            return httpx.Response(302, headers={"location": resolved})
        if path.startswith("/bbcswebdav/"):
            return httpx.Response(
                200,
                content=PDF_BODY,
                headers={
                    "content-type": "application/pdf",
                    "etag": f'"etag-{path}"',
                    "last-modified": "Sat, 27 Jan 2018 09:17:04 GMT",
                },
            )
        return httpx.Response(404, html=fixture("not_found.html"))


def build_session(
    transport: httpx.MockTransport, download_dir: Path
) -> tuple[GuestSession, BlackboardConfig]:
    config = BlackboardConfig(
        base_url=BASE_URL,
        courses=(CourseConfig(id=COURSE_ID),),
        request_delay_seconds=0.0,
        max_retries=0,
    )
    client = httpx.AsyncClient(
        transport=transport, follow_redirects=True, base_url=BASE_URL
    )
    return GuestSession(config, client=client, download_dir=download_dir), config


class BlackboardCrawlTest(unittest.IsolatedAsyncioTestCase):
    async def test_crawl_records_content_and_skips_noise(self) -> None:
        denied = "/bbcswebdav/pid-12968-dt-content-rid-50851_1/xid-50851_1"
        transport = RecordingTransport(resource_status={denied: 403})
        with tempfile.TemporaryDirectory() as temporary_dir:
            download_dir = Path(temporary_dir)
            session, config = build_session(transport, download_dir)
            async with session:
                result = await crawl_course(session, config.courses[0])

            pages = {page.content_id: page for page in result.pages}
            self.assertEqual(pages["_12953_1"].page_kind, PAGE_KIND_CONTENT)
            self.assertTrue(pages["_12953_1"].indexed)
            self.assertEqual(pages["_12953_1"].item_count, 4)

            # Same body under a different content id: recorded, not indexed.
            self.assertEqual(pages["_12954_1"].duplicate_of, "_12953_1")
            self.assertFalse(pages["_12954_1"].indexed)
            self.assertEqual(pages["_12954_1"].exclusion_reason, "duplicate")

            # A content area with no items is navigation scaffolding.
            self.assertEqual(pages["_12955_1"].page_kind, PAGE_KIND_NAVIGATION)
            self.assertFalse(pages["_12955_1"].indexed)

            # Blackboard answers a course the guest may not see with its
            # "resource not found" placeholder.
            self.assertEqual(pages["_12956_1"].page_kind, PAGE_KIND_NOT_FOUND)
            self.assertFalse(pages["_12956_1"].indexed)

            # The folder linked from the indexed page was followed.
            self.assertIn("_12962_1", pages)

            resources = {
                resource.requested_url: resource for resource in result.resources
            }
            self.assertEqual(len(resources), 3)
            downloaded = resources[
                f"{BASE_URL}/bbcswebdav/pid-12958-dt-content-rid-50834_1/xid-50834_1"
            ]
            self.assertEqual(downloaded.access_state, ACCESS_PUBLIC)
            self.assertEqual(downloaded.access_mode, ACCESS_MODE_GUEST)
            self.assertEqual(downloaded.size_bytes, len(PDF_BODY))
            self.assertEqual(downloaded.sha256, PDF_SHA256)
            self.assertIsNotNone(downloaded.local_path)
            self.assertTrue((download_dir / str(downloaded.local_path)).is_file())
            self.assertEqual(downloaded.source_page, pages["_12953_1"].final_url)
            # "Lecture_Intro_2014_Autumn.pdf" says slides on its own; the item
            # named only "第一次实验" is a lab, and the WEEK4-PPT attachment is
            # classified before its content area ("课程PPT") is consulted.
            self.assertEqual(downloaded.category, CATEGORY_SLIDES)
            self.assertEqual(
                resources[
                    f"{BASE_URL}/bbcswebdav/pid-12961-dt-content-rid-50846_1/xid-50846_1"
                ].category,
                CATEGORY_LAB,
            )

    async def test_forbidden_resource_is_recorded_as_auth_required(self) -> None:
        denied = "/bbcswebdav/pid-12968-dt-content-rid-50851_1/xid-50851_1"
        transport = RecordingTransport(resource_status={denied: 403})
        with tempfile.TemporaryDirectory() as temporary_dir:
            session, config = build_session(transport, Path(temporary_dir))
            async with session:
                result = await crawl_course(session, config.courses[0])

            blocked = next(
                resource
                for resource in result.resources
                if resource.requested_url.endswith("xid-50851_1")
            )
            self.assertEqual(blocked.access_state, ACCESS_AUTH_REQUIRED)
            self.assertEqual(blocked.status, 403)
            self.assertIsNone(blocked.final_url)
            self.assertIsNone(blocked.local_path)
            self.assertIsNone(blocked.sha256)

            # The refusal is final: it is requested exactly once, and nothing
            # about that request carries or asks for credentials.
            attempts = [
                request for request in transport.requests if request.url.path == denied
            ]
            self.assertEqual(len(attempts), 1)

    async def test_no_request_ever_carries_credentials(self) -> None:
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as temporary_dir:
            session, config = build_session(transport, Path(temporary_dir))
            async with session:
                await crawl_course(session, config.courses[0])

        self.assertTrue(transport.requests)
        for request in transport.requests:
            self.assertEqual(request.method, "GET")
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(request.read(), b"")
            query = parse_qs(request.url.query.decode())
            for credential_field in ("user_id", "password", "encoded_pw"):
                self.assertNotIn(credential_field, query)

        # The very first request is the site's own anonymous entry point.
        self.assertEqual(transport.requests[0].url.path, "/webapps/login")
        self.assertIn(b"action=guest_login", transport.requests[0].url.query)


class BlackboardBuilderTest(unittest.IsolatedAsyncioTestCase):
    async def _build(self, build_dir: Path, download_dir: Path) -> RecordingTransport:
        transport = RecordingTransport(
            resource_status={
                "/bbcswebdav/pid-12968-dt-content-rid-50851_1/xid-50851_1": 403
            }
        )
        config = BlackboardConfig(
            base_url=BASE_URL,
            courses=(CourseConfig(id=COURSE_ID, name="CUDA"),),
            request_delay_seconds=0.0,
            max_retries=0,
        )
        client = httpx.AsyncClient(
            transport=transport, follow_redirects=True, base_url=BASE_URL
        )
        with patch("src.blackboard.BUILD_DIR", build_dir):
            await make_blackboard(config, client=client, download_dir=download_dir)
        await client.aclose()
        return transport

    async def test_builder_writes_pages_resources_and_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            build_dir = root / "build"
            build_dir.mkdir()
            await self._build(build_dir, root / "downloads")

            snapshot = build_dir / SNAPSHOT_FILENAME
            self.assertTrue(snapshot.is_file())
            connection = sqlite3.connect(snapshot)
            try:
                metadata = dict(
                    connection.execute("SELECT key, value FROM metadata").fetchall()
                )
                resources = connection.execute(
                    "SELECT access_state, category, access_mode "
                    "FROM blackboard_resources"
                ).fetchall()
                indexed = connection.execute(
                    "SELECT COUNT(*) FROM blackboard_pages WHERE indexed = 1"
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(metadata["blackboard_access_mode"], ACCESS_MODE_GUEST)
            self.assertEqual(metadata["blackboard_resource_count"], "3")
            self.assertEqual(metadata["blackboard_auth_required_resource_count"], "1")
            self.assertEqual(metadata["blackboard_public_resource_count"], "2")
            self.assertEqual(indexed, 1)
            self.assertEqual(
                sorted(state for state, _, _ in resources),
                [ACCESS_AUTH_REQUIRED, ACCESS_PUBLIC, ACCESS_PUBLIC],
            )
            self.assertTrue(all(mode == ACCESS_MODE_GUEST for _, _, mode in resources))

    async def test_second_run_revalidates_instead_of_redownloading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            build_dir = root / "build"
            build_dir.mkdir()
            download_dir = root / "downloads"
            await self._build(build_dir, download_dir)

            store = SQLiteModelStore(build_dir / SNAPSHOT_FILENAME, reset=False)
            try:
                previous = load_previous_resources(store)
            finally:
                store.close()

            # Only the resources the guest session was actually served may be
            # revalidated; the refused one must be re-checked from scratch.
            self.assertEqual(len(previous), 2)
            self.assertTrue(all(record["etag"] for record in previous.values()))

            transport = await self._build(build_dir, download_dir)
            conditional = [
                headers for headers in transport.conditional_headers if headers
            ]
            self.assertEqual(len(conditional), 2)
            self.assertTrue(all("if-none-match" in headers for headers in conditional))

            connection = sqlite3.connect(build_dir / SNAPSHOT_FILENAME)
            try:
                revalidated = connection.execute(
                    "SELECT sha256, size_bytes, local_path FROM blackboard_resources "
                    "WHERE not_modified = 1"
                ).fetchall()
                metadata = dict(
                    connection.execute("SELECT key, value FROM metadata").fetchall()
                )
            finally:
                connection.close()

            # A 304 keeps the recorded metadata instead of downloading again.
            self.assertEqual(len(revalidated), 2)
            for sha256, size_bytes, local_path in revalidated:
                self.assertEqual(sha256, PDF_SHA256)
                self.assertEqual(size_bytes, len(PDF_BODY))
                self.assertTrue(local_path)
            self.assertEqual(metadata["blackboard_public_resource_count"], "2")

            # Without the file cache (CI restores only the snapshot), a 304 still
            # keeps the hash and size but must not claim a file that is not there.
            shutil.rmtree(download_dir)
            await self._build(build_dir, download_dir)
            connection = sqlite3.connect(build_dir / SNAPSHOT_FILENAME)
            try:
                without_cache = connection.execute(
                    "SELECT sha256, local_path FROM blackboard_resources "
                    "WHERE not_modified = 1"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(len(without_cache), 2)
            for sha256, local_path in without_cache:
                self.assertEqual(sha256, PDF_SHA256)
                self.assertIsNone(local_path)


class BlackboardConfigTest(unittest.TestCase):
    def test_repository_config_is_loadable_and_guest_only(self) -> None:
        config = load_blackboard_config()

        self.assertTrue(config.courses)
        self.assertEqual(config.base_url, BASE_URL)
        self.assertGreater(config.request_delay_seconds, 0)
        self.assertGreater(config.max_pages_per_course, 0)

    def test_config_without_courses_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "blackboard-config.yaml"
            path.write_text("courses: []\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_blackboard_config(path)
