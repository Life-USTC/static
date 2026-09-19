"""Blackboard anonymous guest-session crawler.

The USTC Blackboard instance exposes a subset of its courses to an anonymous
`guest_login` session. This builder walks those courses, records every page and
downloadable resource it is served, and keeps the noise (the login shell,
duplicate pages and contentless navigation pages) out of the content index.

Boundary: the crawler never submits a username or a password and never tries to
work around an access control. Anything answered with 401/403, or answered with
the login shell, is recorded as `auth_required` and skipped.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
import yaml

from .models.blackboard import (
    ACCESS_AUTH_REQUIRED,
    ACCESS_ERROR,
    ACCESS_MODE_GUEST,
    ACCESS_NOT_FOUND,
    ACCESS_PUBLIC,
    ACCESS_TOO_LARGE,
    CATEGORY_OTHER,
    PAGE_KIND_CONTENT,
    PAGE_KIND_LOGIN,
    PAGE_KIND_NOT_FOUND,
    BlackboardPage,
    BlackboardResource,
)
from .sqlite_store import SNAPSHOT_FILENAME, SQLiteModelStore
from .utils.blackboard_html import (
    classify_page,
    classify_resource,
    filename_from_url,
    is_login_document,
    is_login_url,
    is_not_found_document,
    parse_content_items,
    parse_course_menu,
    parse_course_title,
    parse_page_title,
)
from .utils.tools import BASE_DIR, BUILD_DIR

logger = logging.getLogger(__name__)

BLACKBOARD_CONFIG_PATH = BASE_DIR / "blackboard-config.yaml"
BLACKBOARD_DOWNLOAD_DIR = BASE_DIR / ".artifacts" / "blackboard"

PAGES_SOURCE = "blackboard_pages"
RESOURCES_SOURCE = "blackboard_resources"

# Identifies the project rather than pretending to be a browser: the university
# runs this server and should be able to see who is asking.
USER_AGENT = (
    "Life-USTC-static/0.1 (+https://github.com/Life-USTC/static; "
    "guest-session course material crawler)"
)

LAUNCHER_PATH = "/webapps/blackboard/execute/launcher"
LOGIN_PATH = "/webapps/login"
CONTENT_PATH = "/webapps/blackboard/content/listContent.jsp"

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRY_WAIT_SECONDS = 30.0
_HTML_INSPECT_BYTES = 256 * 1024


@dataclass(frozen=True)
class CourseConfig:
    id: str
    name: str | None = None
    content_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BlackboardConfig:
    base_url: str = "https://www.bb.ustc.edu.cn"
    courses: tuple[CourseConfig, ...] = field(default_factory=tuple)
    request_delay_seconds: float = 1.5
    request_timeout_seconds: float = 60.0
    max_pages_per_course: int = 40
    max_resources_per_course: int = 60
    max_resource_bytes: int = 32 * 1024 * 1024
    max_retries: int = 2


@dataclass
class FetchedPage:
    requested_url: str
    final_url: str
    status: int
    text: str


@dataclass
class FetchedResource:
    requested_url: str
    final_url: str
    status: int
    content_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    local_path: str | None = None
    html_preview: str = ""
    too_large: bool = False


@dataclass
class CrawlResult:
    pages: list[BlackboardPage] = field(default_factory=list)
    resources: list[BlackboardResource] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def guest_login_url(base_url: str, course_id: str) -> str:
    """Build the anonymous guest-session bootstrap URL for a course.

    No username and no password are ever part of this request; `guest_login` is
    the site's own anonymous entry point.
    """

    new_loc = f"{LAUNCHER_PATH}?type=Course&id={course_id}"
    query = urlencode({"action": "guest_login", "new_loc": new_loc})
    return f"{base_url.rstrip('/')}{LOGIN_PATH}?{query}"


def launcher_url(base_url: str, course_id: str) -> str:
    return f"{base_url.rstrip('/')}{LAUNCHER_PATH}?type=Course&id={course_id}"


def list_content_url(base_url: str, *, course_id: str, content_id: str) -> str:
    query = urlencode(
        {"content_id": content_id, "course_id": course_id, "mode": "reset"}
    )
    return f"{base_url.rstrip('/')}{CONTENT_PATH}?{query}"


def load_blackboard_config(path: Path | None = None) -> BlackboardConfig:
    """Load the crawl scope from `blackboard-config.yaml`."""

    config_path = path or BLACKBOARD_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"Blackboard config {config_path} is not a mapping")

    defaults = BlackboardConfig()
    courses: list[CourseConfig] = []
    for entry in raw.get("courses") or []:
        if not isinstance(entry, dict):
            raise TypeError(f"Blackboard config {config_path} has a malformed course")
        course_id = str(entry.get("id") or "").strip()
        if not course_id:
            raise ValueError(f"Blackboard config {config_path} has a course without id")
        name = entry.get("name")
        content_ids = tuple(str(value) for value in entry.get("content_ids") or ())
        courses.append(
            CourseConfig(
                id=course_id,
                name=str(name) if name else None,
                content_ids=content_ids,
            )
        )

    if not courses:
        raise ValueError(f"Blackboard config {config_path} lists no courses")

    return BlackboardConfig(
        base_url=str(raw.get("base_url") or defaults.base_url),
        courses=tuple(courses),
        request_delay_seconds=float(
            raw.get("request_delay_seconds", defaults.request_delay_seconds)
        ),
        request_timeout_seconds=float(
            raw.get("request_timeout_seconds", defaults.request_timeout_seconds)
        ),
        max_pages_per_course=int(
            raw.get("max_pages_per_course", defaults.max_pages_per_course)
        ),
        max_resources_per_course=int(
            raw.get("max_resources_per_course", defaults.max_resources_per_course)
        ),
        max_resource_bytes=int(
            raw.get("max_resource_bytes", defaults.max_resource_bytes)
        ),
        max_retries=int(raw.get("max_retries", defaults.max_retries)),
    )


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return min(max(float(value), 0.0), _MAX_RETRY_WAIT_SECONDS)
    except ValueError:
        pass
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    delay = (date - datetime.now(UTC)).total_seconds()
    return min(max(delay, 0.0), _MAX_RETRY_WAIT_SECONDS)


class GuestSession:
    """A single polite, cookie-keeping, credential-free Blackboard client."""

    def __init__(
        self,
        config: BlackboardConfig,
        *,
        client: httpx.AsyncClient | None = None,
        download_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.download_dir = download_dir or BLACKBOARD_DOWNLOAD_DIR
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                config.request_timeout_seconds,
                connect=min(config.request_timeout_seconds, 15.0),
            ),
            # One connection, one request at a time: this is the university's
            # own teaching server, not a target to saturate.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        self._last_request = 0.0
        self.request_count = 0

    async def __aenter__(self) -> GuestSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _throttle(self) -> None:
        delay = max(0.0, self.config.request_delay_seconds)
        if not delay:
            return
        loop = asyncio.get_running_loop()
        wait_for = delay - (loop.time() - self._last_request)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        self._last_request = loop.time()

    async def start_course_session(self, course_id: str) -> FetchedPage:
        """Establish the anonymous guest session for one course."""

        return await self.fetch_page(guest_login_url(self.config.base_url, course_id))

    async def fetch_page(self, url: str) -> FetchedPage:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            await self._throttle()
            self.request_count += 1
            try:
                response = await self.client.get(url)
            except httpx.HTTPError as error:
                last_error = error
                if attempt < self.config.max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                break
            if (
                response.status_code in _RETRY_STATUSES
                and attempt < self.config.max_retries
            ):
                wait_for = _retry_after_seconds(response.headers.get("retry-after"))
                await asyncio.sleep(wait_for or 2**attempt)
                continue
            return FetchedPage(
                requested_url=url,
                final_url=str(response.url),
                status=response.status_code,
                text=response.text,
            )
        logger.warning("Blackboard page request failed for %s: %s", url, last_error)
        return FetchedPage(requested_url=url, final_url=url, status=0, text="")

    async def fetch_resource(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchedResource:
        """Stream one resource, hashing it as it arrives."""

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        await self._throttle()
        self.request_count += 1
        try:
            async with self.client.stream("GET", url, headers=headers) as response:
                return await self._read_resource(url, response)
        except httpx.HTTPError as error:
            logger.warning("Blackboard resource request failed for %s: %s", url, error)
            return FetchedResource(
                requested_url=url, final_url=url, status=0, content_type=None
            )

    async def _read_resource(
        self, url: str, response: httpx.Response
    ) -> FetchedResource:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        result = FetchedResource(
            requested_url=url,
            final_url=str(response.url),
            status=response.status_code,
            content_type=content_type or None,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
        if response.status_code == 304:
            result.not_modified = True
            return result
        if response.status_code >= 400:
            return result

        is_html = content_type.startswith("text/html")
        digest = hashlib.sha256()
        size = 0
        preview = bytearray()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
            dir=self.download_dir, delete=False
        )
        temporary_path = Path(handle.name)
        try:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.config.max_resource_bytes:
                    result.too_large = True
                    break
                digest.update(chunk)
                handle.write(chunk)
                if is_html and len(preview) < _HTML_INSPECT_BYTES:
                    preview.extend(chunk)
        finally:
            handle.close()

        if result.too_large:
            temporary_path.unlink(missing_ok=True)
            return result

        result.size_bytes = size
        result.sha256 = digest.hexdigest()
        if is_html:
            result.html_preview = preview.decode("utf-8", errors="replace")
        result.local_path = self._store_download(
            temporary_path, sha256=result.sha256, final_url=result.final_url
        )
        return result

    def _store_download(
        self, temporary_path: Path, *, sha256: str, final_url: str
    ) -> str:
        suffix = Path(filename_from_url(final_url) or "").suffix[:16]
        relative = Path("files") / sha256[:2] / f"{sha256}{suffix}"
        destination = self.download_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            temporary_path.unlink(missing_ok=True)
        else:
            os.replace(temporary_path, destination)
        return relative.as_posix()


def _resource_access_state(fetched: FetchedResource) -> str:
    if fetched.status in {401, 403}:
        return ACCESS_AUTH_REQUIRED
    if fetched.status == 404:
        return ACCESS_NOT_FOUND
    if fetched.too_large:
        return ACCESS_TOO_LARGE
    if fetched.status == 0 or fetched.status >= 400:
        return ACCESS_ERROR
    if is_login_url(fetched.final_url):
        return ACCESS_AUTH_REQUIRED
    if fetched.html_preview:
        if is_login_document(fetched.html_preview):
            return ACCESS_AUTH_REQUIRED
        if is_not_found_document(fetched.html_preview):
            return ACCESS_NOT_FOUND
    return ACCESS_PUBLIC


def _page_access_state(page_kind: str) -> str:
    if page_kind == PAGE_KIND_LOGIN:
        return ACCESS_AUTH_REQUIRED
    if page_kind == PAGE_KIND_NOT_FOUND:
        return ACCESS_NOT_FOUND
    return ACCESS_PUBLIC


async def crawl_course(
    session: GuestSession,
    course: CourseConfig,
    *,
    previous_resources: Mapping[str, Mapping[str, Any]] | None = None,
) -> CrawlResult:
    """Crawl one course through the anonymous guest session."""

    config = session.config
    previous_resources = previous_resources or {}
    result = CrawlResult()

    await session.start_course_session(course.id)
    entry = await session.fetch_page(launcher_url(config.base_url, course.id))
    course_title = parse_course_title(entry.text) or course.name

    entry_kind = classify_page(
        status=entry.status, final_url=entry.final_url, html=entry.text, item_count=0
    )
    if entry_kind in {PAGE_KIND_LOGIN, PAGE_KIND_NOT_FOUND}:
        # The guest session is not allowed into this course. Record the refusal
        # and move on; never retry it with credentials.
        result.pages.append(
            BlackboardPage(
                course_id=course.id,
                course_title=course_title,
                requested_url=entry.requested_url,
                final_url=entry.final_url,
                status=entry.status,
                page_kind=entry_kind,
                access_state=_page_access_state(entry_kind),
                exclusion_reason=entry_kind,
                fetched_at=_now(),
            )
        )
        logger.info("Course %s is not readable by the guest session", course.id)
        return result

    queue: list[tuple[str, str | None, int]]
    if course.content_ids:
        queue = [(content_id, None, 1) for content_id in course.content_ids]
    else:
        queue = [
            (area.content_id, area.title, 1)
            for area in parse_course_menu(entry.text, course_id=course.id)
        ]

    visited: set[str] = set()
    body_hashes: dict[str, str] = {}
    resource_urls: set[str] = set()
    resource_budget = config.max_resources_per_course
    pending: list[tuple[str, str, str | None, str | None, str | None]] = []

    while queue and len(visited) < config.max_pages_per_course:
        content_id, area_title, depth = queue.pop(0)
        if content_id in visited:
            continue
        visited.add(content_id)

        url = list_content_url(
            config.base_url, course_id=course.id, content_id=content_id
        )
        page = await session.fetch_page(url)
        items = parse_content_items(page.text, course_id=course.id) if page.text else []
        page_kind = classify_page(
            status=page.status,
            final_url=page.final_url,
            html=page.text,
            item_count=len(items),
        )
        body_sha256 = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
        duplicate_of = body_hashes.get(body_sha256)
        if duplicate_of is None and page_kind == PAGE_KIND_CONTENT:
            body_hashes[body_sha256] = content_id

        exclusion_reason: str | None = None
        if page_kind != PAGE_KIND_CONTENT:
            exclusion_reason = page_kind
        elif duplicate_of is not None:
            exclusion_reason = "duplicate"
        indexed = exclusion_reason is None

        result.pages.append(
            BlackboardPage(
                course_id=course.id,
                course_title=course_title,
                content_id=content_id,
                title=area_title or parse_page_title(page.text),
                requested_url=page.requested_url,
                final_url=page.final_url,
                status=page.status,
                page_kind=page_kind,
                access_state=_page_access_state(page_kind),
                item_count=len(items),
                indexed=indexed,
                exclusion_reason=exclusion_reason,
                body_sha256=body_sha256,
                duplicate_of=duplicate_of,
                source_page=entry.final_url if depth == 1 else None,
                depth=depth,
                fetched_at=_now(),
            )
        )

        if not indexed:
            continue

        for item in items:
            if item.folder_content_id and item.folder_content_id not in visited:
                queue.append((item.folder_content_id, item.title, depth + 1))
            for link in item.resources:
                absolute = urljoin(config.base_url, link.url)
                if absolute in resource_urls or len(pending) >= resource_budget:
                    continue
                resource_urls.add(absolute)
                pending.append(
                    (absolute, page.final_url, item.item_id, item.title, area_title)
                )

    for absolute, source_page, item_id, item_title, area_title in pending:
        cached = previous_resources.get(absolute, {})
        fetched = await session.fetch_resource(
            absolute,
            etag=cached.get("etag"),
            last_modified=cached.get("last_modified"),
        )
        access_state = _resource_access_state(fetched)
        if access_state == ACCESS_AUTH_REQUIRED:
            logger.info("Skipping %s: guest session is not authorised", absolute)

        filename = filename_from_url(fetched.final_url)
        category = classify_resource(item_title, filename)
        if category == CATEGORY_OTHER:
            # The content area name ("Course Materials:课程PPT") is the weakest
            # hint, so it only decides when the item itself says nothing.
            category = classify_resource(area_title)
        if fetched.not_modified:
            filename = cached.get("filename") or filename
            sha256 = cached.get("sha256")
            size_bytes = cached.get("size_bytes")
            # The hash and size stay valid across builds; the cached file
            # itself only counts as present when it is really on disk here.
            local_path = cached.get("local_path")
            if not local_path or not (session.download_dir / local_path).is_file():
                local_path = None
            content_type = fetched.content_type or cached.get("content_type")
            final_url = cached.get("final_url") or fetched.final_url
            access_state = ACCESS_PUBLIC
        else:
            sha256 = fetched.sha256
            size_bytes = fetched.size_bytes
            local_path = fetched.local_path
            content_type = fetched.content_type
            final_url = fetched.final_url

        result.resources.append(
            BlackboardResource(
                course_id=course.id,
                course_title=course_title,
                item_id=item_id,
                title=item_title,
                category=category,
                requested_url=absolute,
                final_url=final_url if access_state != ACCESS_AUTH_REQUIRED else None,
                status=fetched.status,
                content_type=content_type,
                filename=filename,
                size_bytes=size_bytes,
                sha256=sha256,
                local_path=local_path,
                etag=fetched.etag or cached.get("etag"),
                last_modified=fetched.last_modified or cached.get("last_modified"),
                access_mode=ACCESS_MODE_GUEST,
                access_state=access_state,
                not_modified=fetched.not_modified,
                source_page=source_page,
                fetched_at=_now(),
            )
        )

    return result


def _delete_source(store: SQLiteModelStore, source: str) -> None:
    fetch_ids = [
        row[0]
        for row in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ?", (source,)
        )
    ]
    store.delete_fetches(fetch_ids)


def load_previous_resources(store: SQLiteModelStore) -> dict[str, dict[str, Any]]:
    """Read the last run's resource metadata for conditional requests."""

    columns: tuple[str, ...] = (
        "requested_url",
        "final_url",
        "filename",
        "content_type",
        "size_bytes",
        "sha256",
        "local_path",
        "etag",
        "last_modified",
        "access_state",
    )
    try:
        rows = store.conn.execute(
            f"SELECT {', '.join(columns)} FROM {RESOURCES_SOURCE}"
        ).fetchall()
    except sqlite3.DatabaseError:
        # A snapshot written before this builder existed has no such table.
        return {}

    previous: dict[str, dict[str, Any]] = {}
    for row in rows:
        record: dict[str, Any] = dict(zip(columns, row, strict=True))
        url = record.get("requested_url")
        # Only a previously public resource may be revalidated; an
        # auth_required row must be re-checked from scratch, never cached.
        if isinstance(url, str) and record.get("access_state") == ACCESS_PUBLIC:
            previous[url] = record
    return previous


def _store_results(
    store: SQLiteModelStore,
    *,
    config: BlackboardConfig,
    results: Mapping[str, CrawlResult],
) -> dict[str, int]:
    store.register_response_model(
        table_name=PAGES_SOURCE, response_model=BlackboardPage
    )
    store.register_response_model(
        table_name=RESOURCES_SOURCE, response_model=BlackboardResource
    )

    counts = {
        "pages": 0,
        "indexed_pages": 0,
        "resources": 0,
        "public_resources": 0,
        "auth_required_resources": 0,
    }
    for course_id, result in results.items():
        pages_fetch_id = store.record_fetch(
            source=PAGES_SOURCE,
            method="GET",
            url=launcher_url(config.base_url, course_id),
            context={"course_id": course_id, "access_mode": ACCESS_MODE_GUEST},
        )
        for page in result.pages:
            store.store_response(
                table_name=PAGES_SOURCE, response=page, fetch_id=pages_fetch_id
            )
            counts["pages"] += 1
            counts["indexed_pages"] += int(page.indexed)

        resources_fetch_id = store.record_fetch(
            source=RESOURCES_SOURCE,
            method="GET",
            url=launcher_url(config.base_url, course_id),
            context={"course_id": course_id, "access_mode": ACCESS_MODE_GUEST},
        )
        for resource in result.resources:
            store.store_response(
                table_name=RESOURCES_SOURCE,
                response=resource,
                fetch_id=resources_fetch_id,
            )
            counts["resources"] += 1
            counts["public_resources"] += int(resource.access_state == ACCESS_PUBLIC)
            counts["auth_required_resources"] += int(
                resource.access_state == ACCESS_AUTH_REQUIRED
            )
    return counts


async def make_blackboard(
    config: BlackboardConfig | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    download_dir: Path | None = None,
) -> None:
    """Crawl the configured Blackboard courses into the static snapshot."""

    config = config or load_blackboard_config()
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = BUILD_DIR / SNAPSHOT_FILENAME
    reuse_snapshot = snapshot_path.exists()

    with tempfile.TemporaryDirectory(
        dir=BUILD_DIR, prefix="blackboard-refresh-"
    ) as temporary_dir:
        staged_snapshot_path = Path(temporary_dir) / SNAPSHOT_FILENAME
        if reuse_snapshot:
            shutil.copy2(snapshot_path, staged_snapshot_path)

        store = SQLiteModelStore(staged_snapshot_path, reset=not reuse_snapshot)
        try:
            previous_resources = load_previous_resources(store)
            results: dict[str, CrawlResult] = {}
            async with GuestSession(
                config, client=client, download_dir=download_dir
            ) as session:
                for course in config.courses:
                    logger.info("Crawling Blackboard course %s", course.id)
                    results[course.id] = await crawl_course(
                        session,
                        course,
                        previous_resources=previous_resources,
                    )
                request_count = session.request_count

            _delete_source(store, PAGES_SOURCE)
            _delete_source(store, RESOURCES_SOURCE)
            counts = _store_results(store, config=config, results=results)
            store.put_metadata(
                {
                    "blackboard_access_mode": ACCESS_MODE_GUEST,
                    "blackboard_synced_at": _now(),
                    "blackboard_course_count": len(config.courses),
                    "blackboard_request_count": request_count,
                    "blackboard_page_count": counts["pages"],
                    "blackboard_indexed_page_count": counts["indexed_pages"],
                    "blackboard_resource_count": counts["resources"],
                    "blackboard_public_resource_count": counts["public_resources"],
                    "blackboard_auth_required_resource_count": counts[
                        "auth_required_resources"
                    ],
                }
            )
            logger.info(
                "Blackboard guest crawl stored %s page(s), %s indexed, "
                "%s resource(s), %s auth_required",
                counts["pages"],
                counts["indexed_pages"],
                counts["resources"],
                counts["auth_required_resources"],
            )
        finally:
            store.close()

        os.replace(staged_snapshot_path, snapshot_path)
