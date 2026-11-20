"""Collect public teaching profiles keyed by semester and section code."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from .models.api.catalog_api_teach_lesson_infos import (
    TeachLessonInfo,
    TeachLessonInfosResponse,
)
from .models.api.catalog_api_teach_lesson_list_for_teach import TeachLessonListResponse
from .models.api.catalog_api_teach_semester_list import TeachSemesterListResponse
from .sqlite_store import SQLiteModelStore
from .upstream_contracts import CATALOG_LESSON_INFOS, CATALOG_LESSONS
from .utils.auth import RequestSession

API = "https://catalog.ustc.edu.cn/api/teach"
SEMESTER_LIST_URL = f"{API}/semester/list"
LESSON_LIST_URL = f"{API}/lesson/list-for-teach"
LESSON_INFOS_URL = f"{API}/lesson/infos"

CATALOG_PUBLIC_SEMESTERS = "catalog_teach_public_semester_list"
CATALOG_PUBLIC_LESSONS = "catalog_teach_public_lesson_list_for_teach"

BATCH_SIZE = 100
MAX_PROFILE_REQUESTS = 3
REQUEST_INTERVAL_SECONDS = 0.2
MISSING_PROBE_LIMIT = 6
ACTIVE_REFRESH_AGE = timedelta(hours=12)
RECENT_TERM_REFRESH_AGE = timedelta(days=7)
RECENT_TERM_WINDOW = timedelta(days=120)
SETTLED_TERM_REFRESH_AGE = timedelta(days=30)
REQUEST_OPTIONS = {"timeout": 60_000, "transient_retries": 0}
HISTORIC_NOT_FOUND = {404, 410}
TIME_ZONE = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Semester:
    id: int
    start: date
    end: date
    published: bool

    def needs_public_index(self, today: date) -> bool:
        return self.start <= today and (self.end >= today or self.published)


@dataclass(frozen=True)
class LessonIdentity:
    id: int
    course_id: int | None


class LessonIndexMismatchError(ValueError):
    """The profile endpoint did not corroborate the selected index."""


def _semester_key(semester_id: int) -> str:
    return f"catalog_semester_details_{semester_id}"


def _has_table(store: SQLiteModelStore, table: str) -> bool:
    return bool(
        store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type = 'table'",
            (table,),
        ).fetchone()
    )


def _previous_snapshot(store: SQLiteModelStore, semester_id: int) -> dict:
    row = store.conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (_semester_key(semester_id),)
    ).fetchone()
    return json.loads(row[0]) if row else {}


def _replace_source(
    store: SQLiteModelStore,
    source: str,
    response: BaseModel | None,
    response_model: type[BaseModel],
    *,
    url: str,
    method: str = "GET",
    semester_id: int | None = None,
) -> None:
    context = {"semester_id": str(semester_id)} if semester_id is not None else None
    context_value = f"semester_id={semester_id}" if context is not None else None
    old = [
        fetch_id
        for (fetch_id,) in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ? AND context IS ?",
            (source, context_value),
        )
    ]
    store.register_response_model(table_name=source, response_model=response_model)
    store.delete_fetches(old)
    fetch_id = store.record_fetch(
        source=source,
        method=method,
        url=url,
        context=context,
        ok=response is not None,
        error="Public history unavailable" if response is None else None,
    )
    if response is not None:
        store.store_response(
            table_name=source, response=response, fetch_id=fetch_id, context=context
        )


def _read_lesson_indexes(
    store: SQLiteModelStore, source: str
) -> dict[int, dict[str, LessonIdentity]]:
    if "semester_id" not in {
        row[1] for row in store.conn.execute(f"PRAGMA table_info({source})")
    }:
        return {}
    course_table = f"{source}_course"
    join = (
        f"LEFT JOIN {course_table} c ON c.parent_store_id = l.store_id"
        if _has_table(store, course_table)
        else ""
    )
    course_column = "c.id" if join else "NULL"
    result: dict[int, dict[str, LessonIdentity]] = {}
    for semester_id, code, lesson_id, course_id in store.conn.execute(
        f"SELECT l.semester_id, l.code, l.id, {course_column} FROM {source} l {join}"
    ):
        if not semester_id:
            continue
        items = result.setdefault(int(semester_id), {})
        _add_lesson(items, code, lesson_id, course_id)
    return result


def _stored_index_times(
    store: SQLiteModelStore,
    source: str,
    indexes: dict[int, dict[str, LessonIdentity]],
    now: datetime,
) -> dict[int, datetime]:
    if not indexes:
        return {}
    fetches = {
        context: (fetch_id, method, url, ok, fetched_at)
        for fetch_id, context, method, url, ok, fetched_at in store.conn.execute(
            "SELECT MIN(id), context, method, url, ok, fetched_at "
            "FROM upstream_fetches WHERE source = ? "
            "GROUP BY context HAVING COUNT(*) = 1",
            (source,),
        )
    }
    result = {}
    for term, first_fetch, last_fetch, count in store.conn.execute(
        f"SELECT semester_id, MIN(fetch_id), MAX(fetch_id), COUNT(*) "
        f"FROM {source} GROUP BY semester_id HAVING COUNT(fetch_id) = COUNT(*)"
    ):
        if not term or first_fetch is None or first_fetch != last_fetch:
            continue
        term_id = int(term)
        lessons = indexes.get(term_id, {})
        fetch = fetches.get(f"semester_id={term_id}")
        if (
            len(lessons) != count
            or len({identity.id for identity in lessons.values()}) != count
            or fetch is None
            or fetch[:4] != (first_fetch, "GET", f"{LESSON_LIST_URL}/{term_id}", 1)
        ):
            continue
        observed_at = _observed_at(fetch[4])
        if observed_at is not None and observed_at <= now:
            result[term_id] = observed_at
    return result


def _verified_curriculum_times(
    store: SQLiteModelStore,
    indexes: dict[int, dict[str, LessonIdentity]],
    now: datetime,
) -> dict[int, datetime]:
    if not indexes:
        return {}
    metadata = dict(
        store.conn.execute(
            "SELECT key, value FROM metadata WHERE key IN ("
            "'curriculum_fetch_status', 'curriculum_successful_semester_ids', "
            "'curriculum_unavailable_semester_ids')"
        )
    )
    if metadata.get("curriculum_fetch_status") not in {
        "complete",
        "partial_sources_unavailable",
    }:
        return {}
    covered = set(metadata.get("curriculum_successful_semester_ids", "").split(","))
    covered -= set(metadata.get("curriculum_unavailable_semester_ids", "").split(","))
    return {
        term_id: observed_at
        for term_id, observed_at in _stored_index_times(
            store, CATALOG_LESSONS, indexes, now
        ).items()
        if str(term_id) in covered
        and all(
            identity.course_id is not None for identity in indexes[term_id].values()
        )
    }


def _add_lesson(
    items: dict[str, LessonIdentity],
    code: str | None,
    lesson_id: int | None,
    course_id: int | None,
) -> None:
    if not code or code != code.strip() or lesson_id is None or lesson_id <= 0:
        raise ValueError("The semester lesson index has an invalid lesson code or ID")
    identity = LessonIdentity(
        lesson_id, course_id if course_id and course_id > 0 else None
    )
    if code in items and items[code] != identity:
        raise ValueError(f"The semester lesson index has conflicting rows for {code!r}")
    items[code] = identity


def _lesson_signature(lessons: dict[str, LessonIdentity]) -> str:
    digest = hashlib.sha256()
    for code, identity in sorted(lessons.items()):
        digest.update(
            json.dumps(
                [code, identity.id, identity.course_id], ensure_ascii=False
            ).encode()
        )
    return digest.hexdigest()


def _observed_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        observed_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return observed_at if observed_at.tzinfo is not None else None


def _snapshot_age(value: object, now: datetime) -> timedelta | None:
    observed_at = _observed_at(value)
    if observed_at is None or observed_at > now:
        return None
    return now - observed_at


def _can_reuse(
    snapshot: dict, signature: str, semester: Semester, now: datetime
) -> bool:
    if snapshot.get("lesson_signature") != signature:
        return False
    age = _snapshot_age(snapshot.get("refreshed_at"), now)
    if age is None:
        return False
    today = now.astimezone(TIME_ZONE).date()
    if semester.needs_public_index(today):
        return age < ACTIVE_REFRESH_AGE
    if today - semester.end <= RECENT_TERM_WINDOW:
        return age < RECENT_TERM_REFRESH_AGE
    if snapshot.get("lesson_infos_unavailable") or snapshot.get(
        "unavailable_lesson_codes"
    ):
        return age < RECENT_TERM_REFRESH_AGE
    return age < SETTLED_TERM_REFRESH_AGE


async def _get_json(session: RequestSession, url: str):
    await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
    return await session.get_json(url, **REQUEST_OPTIONS)


async def _profile_batch(
    session: RequestSession,
    semester_id: int,
    codes: list[str],
    lessons: dict[str, LessonIdentity],
) -> dict[str, TeachLessonInfo]:
    await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
    payload = await session.post_json(
        LESSON_INFOS_URL,
        data={"codes": codes, "semester": semester_id},
        **REQUEST_OPTIONS,
    )
    wanted = set(codes)
    result: dict[str, TeachLessonInfo] = {}
    for row in TeachLessonInfosResponse.model_validate(payload).root:
        if row.code not in wanted or row.code in result:
            raise LessonIndexMismatchError(
                f"Unexpected or repeated lesson profile code {row.code!r}"
            )
        expected = lessons[row.code]
        if row.id != expected.id or (
            expected.course_id is not None and row.course_id != expected.course_id
        ):
            raise LessonIndexMismatchError(
                f"Profile identity differs from the lesson index: {row.code!r}"
            )
        result[row.code] = row
    return result


async def _profile_batches(
    session: RequestSession,
    semester_id: int,
    requested: list[str],
    lessons: dict[str, LessonIdentity],
    allow_historic_absence: bool,
) -> AsyncIterator[tuple[list[str], dict[str, TeachLessonInfo] | None]]:
    batches = iter(batched(requested, BATCH_SIZE, strict=False))
    first = list(next(batches, ()))
    if not first:
        return
    try:
        rows = await _profile_batch(session, semester_id, first, lessons)
    except httpx.HTTPStatusError as error:
        if (
            not allow_historic_absence
            or error.response.status_code not in HISTORIC_NOT_FOUND
        ):
            raise
        try:
            await _profile_batch(session, semester_id, first[:1], lessons)
        except httpx.HTTPStatusError as probe_error:
            if probe_error.response.status_code in HISTORIC_NOT_FOUND:
                yield first, None
                return
            raise
        raise error
    yield first, rows

    async def fetch_after(position: int, codes: list[str]):
        await asyncio.sleep(position * REQUEST_INTERVAL_SECONDS)
        return await _profile_batch(session, semester_id, codes, lessons)

    for window in batched(batches, MAX_PROFILE_REQUESTS, strict=False):
        groups = [list(group) for group in window]
        results = await asyncio.gather(
            *(fetch_after(i, group) for i, group in enumerate(groups)),
            return_exceptions=True,
        )
        for group, result in zip(groups, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            yield group, result


async def _lesson_profiles(
    session: RequestSession,
    semester: Semester,
    lessons: dict[str, LessonIdentity],
    *,
    allow_historic_absence: bool,
    require_complete: bool = False,
) -> tuple[TeachLessonInfosResponse | None, list[str]]:
    records: dict[str, TeachLessonInfo] = {}
    absent: list[str] = []
    probes_left = MISSING_PROBE_LIMIT
    requested = sorted(lessons)
    async for codes, part in _profile_batches(
        session, semester.id, requested, lessons, allow_historic_absence
    ):
        if part is None:
            return None, []
        missing = [code for code in codes if code not in part]
        if missing:
            if require_complete:
                raise LessonIndexMismatchError(
                    f"Lesson profiles omitted curriculum index codes for {semester.id}"
                )
            part.update(await _profile_batch(session, semester.id, missing, lessons))
            still_missing = [code for code in missing if code not in part]
            if len(still_missing) > probes_left:
                raise ValueError(
                    f"Incomplete lesson profile batch for semester {semester.id}"
                )
            for code in still_missing:
                probes_left -= 1
                if len(codes) > 1:
                    part.update(
                        await _profile_batch(session, semester.id, [code], lessons)
                    )
                if code not in part:
                    absent.append(code)
        records.update(part)

    if requested and not records:
        raise ValueError(
            f"Public lesson profiles were empty for semester {semester.id}"
        )
    return TeachLessonInfosResponse(
        [records[code] for code in requested if code in records]
    ), absent


def _semesters(response: TeachSemesterListResponse) -> dict[int, Semester]:
    if not response.root:
        raise ValueError("Public semester index was empty")
    result: dict[int, Semester] = {}
    seen: set[int] = set()
    for term in response.root:
        if term.id is None or term.id <= 0 or term.id in seen:
            raise ValueError("Public semester index contains invalid or duplicate IDs")
        seen.add(term.id)
        if term.start is None or term.end is None:
            continue
        start, end = date.fromisoformat(term.start), date.fromisoformat(term.end)
        if end < start:
            raise ValueError(f"Invalid semester dates for {term.id}")
        result[term.id] = Semester(term.id, start, end, term.isLast is True)
    return result


async def _read_public_index(
    session: RequestSession, semester_id: int, *, allow_historic_absence: bool
) -> tuple[TeachLessonListResponse, dict[str, LessonIdentity]] | None:
    try:
        payload = await _get_json(session, f"{LESSON_LIST_URL}/{semester_id}")
    except httpx.HTTPStatusError as error:
        if allow_historic_absence and error.response.status_code in HISTORIC_NOT_FOUND:
            return None
        raise
    response = TeachLessonListResponse.model_validate(payload)
    if response.root == [] and allow_historic_absence:
        return None
    if not response.root:
        raise ValueError(f"Public lesson list for semester {semester_id} was empty")
    lessons: dict[str, LessonIdentity] = {}
    for row in response.root:
        _add_lesson(lessons, row.code, row.id, row.course.id if row.course else None)
    return response, lessons


async def _fetch_public_index(
    session: RequestSession,
    semester_id: int,
    cached: dict[str, LessonIdentity],
    *,
    allow_historic_absence: bool,
) -> tuple[TeachLessonListResponse | None, dict[str, LessonIdentity]]:
    result = await _read_public_index(
        session, semester_id, allow_historic_absence=allow_historic_absence
    )
    if result is None:
        result = await _read_public_index(
            session, semester_id, allow_historic_absence=allow_historic_absence
        )
    if result is None:
        return None, cached
    response, lessons = result
    if cached and len(lessons) * 100 < len(cached) * 95:
        verified = await _read_public_index(
            session, semester_id, allow_historic_absence=False
        )
        removed = sorted(cached.keys() - lessons.keys())
        controls = list(dict.fromkeys([removed[0], removed[-1]]))
        if (
            verified is None
            or verified[1] != lessons
            or await _profile_batch(session, semester_id, controls, cached)
        ):
            raise ValueError(
                f"Public lesson index for {semester_id} shrank from "
                f"{len(cached)} to {len(lessons)} without corroboration"
            )
    return response, lessons


def _store_public_index(
    store: SQLiteModelStore, semester_id: int, response: TeachLessonListResponse | None
) -> None:
    if (
        response is None
        and store.conn.execute(
            "SELECT 1 FROM upstream_fetches "
            "WHERE source = ? AND context = ? AND ok = 1",
            (CATALOG_PUBLIC_LESSONS, f"semester_id={semester_id}"),
        ).fetchone()
    ):
        return
    _replace_source(
        store,
        CATALOG_PUBLIC_LESSONS,
        response,
        TeachLessonListResponse,
        url=f"{LESSON_LIST_URL}/{semester_id}",
        semester_id=semester_id,
    )


def _index_needs_retry(
    snapshot: dict, now: datetime, newer_curriculum_at: datetime | None
) -> bool:
    if not snapshot:
        return newer_curriculum_at is not None
    maximum_age = (
        RECENT_TERM_REFRESH_AGE
        if snapshot.get("lesson_index_source") in ("cached", "unavailable")
        else SETTLED_TERM_REFRESH_AGE
    )
    checked_at = _observed_at(snapshot.get("lesson_index_checked_at"))
    return (
        checked_at is None
        or checked_at > now
        or now - checked_at >= maximum_age
        or (newer_curriculum_at is not None and newer_curriculum_at > checked_at)
    )


def _ensure_profile_indexes(store: SQLiteModelStore) -> None:
    if "semester_id" not in {
        row[1]
        for row in store.conn.execute(f"PRAGMA table_info({CATALOG_LESSON_INFOS})")
    }:
        return
    store.conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS catalog_lesson_infos_semester_code "
        f"ON {CATALOG_LESSON_INFOS}(semester_id, code)"
    )
    store.conn.execute(
        "CREATE INDEX IF NOT EXISTS catalog_lesson_infos_course_semester "
        f"ON {CATALOG_LESSON_INFOS}(courseId, semester_id)"
    )


async def _refresh_semester(
    session: RequestSession,
    store: SQLiteModelStore,
    semester: Semester,
    cached_index: dict[str, LessonIdentity],
    now: datetime,
    *,
    requested: bool,
    verified_curriculum: bool,
    newer_diverging_curriculum_at: datetime | None,
) -> bool:
    today = now.astimezone(TIME_ZONE).date()
    active = semester.needs_public_index(today)
    ended = semester.end < today and not active
    settled = ended and today - semester.end > RECENT_TERM_WINDOW
    previous = _previous_snapshot(store, semester.id)
    recheck_index = _index_needs_retry(previous, now, newer_diverging_curriculum_at)
    try_curriculum = (
        not requested
        and settled
        and verified_curriculum
        and not recheck_index
        and previous.get("lesson_index_source") not in ("cached", "unavailable")
    )
    if (
        not requested
        and settled
        and _can_reuse(previous, _lesson_signature(cached_index), semester, now)
        and not recheck_index
        and (previous.get("lesson_index_source") != "curriculum" or try_curriculum)
    ):
        return True

    profiles, absent = None, []
    if try_curriculum:
        try:
            profiles, absent = await _lesson_profiles(
                session,
                semester,
                cached_index,
                allow_historic_absence=False,
                require_complete=True,
            )
        except LessonIndexMismatchError as error:
            LOGGER.info(
                "Checking the live index after a curriculum mismatch: %s", error
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in HISTORIC_NOT_FOUND:
                raise

    backed_by_curriculum = profiles is not None
    if backed_by_curriculum:
        public_index, lessons = None, cached_index
        index_source = "curriculum"
    else:
        public_index, lessons = await _fetch_public_index(
            session, semester.id, cached_index, allow_historic_absence=ended
        )
        index_source = (
            "public" if public_index else ("cached" if lessons else "unavailable")
        )
    index_fields = {
        "lesson_index_source": index_source,
        "lesson_index_checked_at": now.isoformat(),
    }
    if public_index is None and not backed_by_curriculum:
        LOGGER.warning(
            "Official lesson index unavailable for semester %s (%s)",
            semester.id,
            index_source,
        )
    signature = _lesson_signature(lessons)
    key = _semester_key(semester.id)
    if _can_reuse(previous, signature, semester, now):
        with store.conn:
            if not backed_by_curriculum:
                _store_public_index(store, semester.id, public_index)
            store.put_metadata(
                {key: json.dumps({**previous, **index_fields}, ensure_ascii=False)}
            )
            _ensure_profile_indexes(store)
        return not requested or (
            public_index is not None and not previous.get("lesson_infos_unavailable")
        )

    if lessons and not backed_by_curriculum:
        profiles, absent = await _lesson_profiles(
            session,
            semester,
            lessons,
            allow_historic_absence=ended and not previous.get("lesson_profile_rows"),
        )
    count = len(profiles.root) if profiles is not None else 0
    if profiles is not None and count + len(absent) != len(lessons):
        raise ValueError(f"Lesson profile coverage did not reconcile for {semester.id}")
    if count == 0 and previous.get("lesson_profile_rows", 0) > 0:
        raise ValueError(
            f"Existing lesson profiles became empty for semester {semester.id}"
        )

    with store.conn:
        if not backed_by_curriculum:
            _store_public_index(store, semester.id, public_index)
        _replace_source(
            store,
            CATALOG_LESSON_INFOS,
            profiles,
            TeachLessonInfosResponse,
            url=LESSON_INFOS_URL,
            method="POST",
            semester_id=semester.id,
        )
        _ensure_profile_indexes(store)
        store.put_metadata(
            {
                key: json.dumps(
                    {
                        **index_fields,
                        "lesson_signature": signature,
                        "lesson_count": len(lessons),
                        "lesson_profile_rows": count,
                        "unavailable_lesson_codes": absent,
                        "lesson_infos_unavailable": profiles is None,
                        "refreshed_at": now.isoformat(),
                    },
                    ensure_ascii=False,
                ),
            }
        )
    if profiles is None:
        LOGGER.warning(
            "Official lesson profiles unavailable for semester %s", semester.id
        )
    return not requested or (public_index is not None and profiles is not None)


async def refresh_catalog_semester_details(
    session: RequestSession,
    store: SQLiteModelStore,
    *,
    semester_ids: Collection[int] | None = None,
) -> None:
    """Refresh currently published terms and history with verified lesson indexes."""
    now = datetime.now(UTC)
    response = TeachSemesterListResponse.model_validate(
        await _get_json(session, SEMESTER_LIST_URL)
    )
    semesters = _semesters(response)
    with store.conn:
        _replace_source(
            store,
            CATALOG_PUBLIC_SEMESTERS,
            response,
            TeachSemesterListResponse,
            url=SEMESTER_LIST_URL,
        )
    curriculum_indexes = _read_lesson_indexes(store, CATALOG_LESSONS)
    public_indexes = _read_lesson_indexes(store, CATALOG_PUBLIC_LESSONS)
    indexes = {**curriculum_indexes, **public_indexes}
    today = now.astimezone(TIME_ZONE).date()
    if semester_ids is not None:
        requested = set(semester_ids)
        unknown = requested - semesters.keys()
        if not requested:
            raise ValueError("At least one explicit semester ID is required")
        if unknown:
            raise ValueError(
                f"Unknown or undated public semester IDs: {sorted(unknown)}"
            )
        selected = [semesters[term_id] for term_id in requested]
    else:
        selected = [
            term
            for term in semesters.values()
            if term.start <= today
            and (term.needs_public_index(today) or term.id in indexes)
        ]
    curriculum_times = (
        _verified_curriculum_times(store, curriculum_indexes, now)
        if semester_ids is None
        else {}
    )
    public_times = _stored_index_times(
        store, CATALOG_PUBLIC_LESSONS, public_indexes, now
    )
    diverging_newer_curriculum = {
        term_id: observed_at
        for term_id, observed_at in curriculum_times.items()
        if term_id in public_indexes
        and curriculum_indexes[term_id] != public_indexes[term_id]
        and (term_id not in public_times or observed_at > public_times[term_id])
    }
    if not selected:
        raise ValueError("Public semester index provided no active or cached semesters")
    failures: list[int] = []
    for semester in sorted(
        selected, key=lambda term: (term.start, term.id), reverse=True
    ):
        try:
            available = await _refresh_semester(
                session,
                store,
                semester,
                indexes.get(semester.id, {}),
                now,
                requested=semester_ids is not None,
                verified_curriculum=semester.id in curriculum_times
                and semester.id not in public_indexes,
                newer_diverging_curriculum_at=diverging_newer_curriculum.get(
                    semester.id
                ),
            )
            if not available:
                failures.append(semester.id)
        except (httpx.HTTPError, ValueError) as error:
            LOGGER.warning(
                "Public semester %s refresh failed; last committed snapshot kept: %s",
                semester.id,
                error,
            )
            failures.append(semester.id)
    if failures:
        raise RuntimeError(f"Public semester details refresh incomplete: {failures}")
