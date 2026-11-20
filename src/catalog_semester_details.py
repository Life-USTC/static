"""Collect public teaching profiles keyed by semester and section code."""

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from zoneinfo import ZoneInfo

import httpx

from .models.api.catalog_api_teach_lesson_infos import TeachLessonInfosResponse
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
OPTIONS = {"timeout": 60_000, "transient_retries": 0}
HISTORIC_NOT_FOUND = {404, 410}
TIME_ZONE = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)
SOURCES = {
    CATALOG_PUBLIC_SEMESTERS: (TeachSemesterListResponse, "GET", SEMESTER_LIST_URL),
    CATALOG_PUBLIC_LESSONS: (
        TeachLessonListResponse,
        "GET",
        LESSON_LIST_URL + "/{term}",
    ),
    CATALOG_LESSON_INFOS: (TeachLessonInfosResponse, "POST", LESSON_INFOS_URL),
}
PROFILE_INDEXES = {
    "catalog_lesson_infos_semester_code": "UNIQUE:semester_id, code",
    "catalog_lesson_infos_course_semester": ":courseId, semester_id",
}


@dataclass(frozen=True)
class LessonIdentity:
    id: int
    course_id: int | None


class LessonIndexMismatchError(ValueError):
    """The profile endpoint did not corroborate the selected index."""


def _time(value, now):
    try:
        observed = datetime.fromisoformat(value)
        return observed if observed.tzinfo and observed <= now else None
    except (TypeError, ValueError):
        return None


def _index(rows):
    result = {}
    for code, lesson, course in rows:
        if not code or code != code.strip() or lesson is None or lesson <= 0:
            raise ValueError(
                "The semester lesson index has an invalid lesson code or ID"
            )
        identity = LessonIdentity(lesson, course if course and course > 0 else None)
        if result.get(code, identity) != identity:
            raise ValueError(
                f"The semester lesson index has conflicting rows for {code!r}"
            )
        result[code] = identity
    return result


def _stored_indexes(store, source):
    db = store.conn
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({source})")}
    if "semester_id" not in columns:
        return {}, set()
    parent = source + "_course"
    has_course = bool(db.execute(f"PRAGMA table_info({parent})").fetchone())
    join = (
        f"LEFT JOIN {parent} c ON c.parent_store_id = l.store_id" if has_course else ""
    )
    course = "c.id" if has_course else "NULL"
    grouped = defaultdict(list)
    for term, code, lesson, course_id in db.execute(
        f"SELECT l.semester_id, l.code, l.id, {course} FROM {source} l {join}"
    ):
        if term:
            grouped[int(term)].append((code, lesson, course_id))
    indexes = {term: _index(rows) for term, rows in grouped.items()}
    if source != CATALOG_LESSONS:
        return indexes, set()

    receipts = db.execute(
        f"""
        WITH receipts AS (
            SELECT id, context, method, url, ok FROM upstream_fetches
            WHERE source = ? GROUP BY context HAVING COUNT(*) = 1
        )
        SELECT l.semester_id, COUNT(*)
        FROM {source} l JOIN receipts f ON f.id = l.fetch_id
        WHERE f.context = 'semester_id=' || l.semester_id
          AND f.url = ? || '/' || l.semester_id AND f.method = 'GET' AND f.ok = 1
        GROUP BY l.semester_id HAVING COUNT(DISTINCT f.id) = 1
        """,
        (source, LESSON_LIST_URL),
    )
    verified = set()
    for semester, count in receipts:
        term = int(semester)
        lessons = indexes[term]
        if (
            len(grouped[term]) == count == len(lessons)
            and len({item.id for item in lessons.values()}) == count
            and all(item.course_id is not None for item in lessons.values())
        ):
            verified.add(term)
    return indexes, verified


def _replace(store, source, response, term=None):
    model, method, url = SOURCES[source]
    context = {"semester_id": str(term)} if term is not None else None
    context_key = f"semester_id={term}" if context else None
    rows = store.conn.execute(
        "SELECT id, ok FROM upstream_fetches WHERE source = ? AND context IS ?",
        (source, context_key),
    ).fetchall()
    if (
        source == CATALOG_PUBLIC_LESSONS
        and response is None
        and any(ok for _, ok in rows)
    ):
        return
    store.register_response_model(table_name=source, response_model=model)
    store.delete_fetches([id_ for id_, _ in rows])
    url = url.format(term=term)
    if response is None:
        store.record_fetch(
            source=source,
            method=method,
            url=url,
            context=context,
            ok=False,
            error="Public history unavailable",
        )
    else:
        store.store_fetched_response(
            source, url, response, method=method, context=context, row_context=context
        )


async def _get(session, url):
    await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
    return await session.get_json(url, **OPTIONS)


async def _profile_batch(session, term, codes, lessons):
    await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
    payload = await session.post_json(
        LESSON_INFOS_URL, data={"codes": codes, "semester": term}, **OPTIONS
    )
    result = {}
    for row in TeachLessonInfosResponse.model_validate(payload).root:
        if row.code not in codes or row.code in result:
            raise LessonIndexMismatchError(
                f"Unexpected or repeated lesson profile code {row.code!r}"
            )
        expected = lessons[row.code]
        if row.id != expected.id or expected.course_id not in (None, row.course_id):
            raise LessonIndexMismatchError(
                f"Profile identity differs from the lesson index: {row.code!r}"
            )
        result[row.code] = row
    return result


async def _profile_batches(session, term, requested, lessons, allow_historic_absence):
    batches = iter(batched(requested, BATCH_SIZE, strict=False))
    first = list(next(batches, ()))
    if not first:
        return
    try:
        yield first, await _profile_batch(session, term, first, lessons)
    except httpx.HTTPStatusError as error:
        if (
            not allow_historic_absence
            or error.response.status_code not in HISTORIC_NOT_FOUND
        ):
            raise
        try:
            await _profile_batch(session, term, first[:1], lessons)
        except httpx.HTTPStatusError as retry:
            if retry.response.status_code in HISTORIC_NOT_FOUND:
                yield first, None
                return
            raise
        raise error

    async def fetch(position, group):
        await asyncio.sleep(position * REQUEST_INTERVAL_SECONDS)
        return await _profile_batch(session, term, group, lessons)

    for window in batched(batches, MAX_PROFILE_REQUESTS, strict=False):
        groups = [list(group) for group in window]
        results = await asyncio.gather(
            *(fetch(position, group) for position, group in enumerate(groups)),
            return_exceptions=True,
        )
        for group, result in zip(groups, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            yield group, result


async def _profiles(session, term, lessons, *, allow_absence=False, complete=False):
    records, absent = {}, []
    budget = MISSING_PROBE_LIMIT
    requested = sorted(lessons)
    async for codes, part in _profile_batches(
        session, term, requested, lessons, allow_absence
    ):
        if part is None:
            return None, []
        missing = [code for code in codes if code not in part]
        if missing and complete:
            raise LessonIndexMismatchError(
                f"Lesson profiles omitted curriculum index codes for {term}"
            )
        if missing:
            part.update(await _profile_batch(session, term, missing, lessons))
            missing = [code for code in missing if code not in part]
            budget -= len(missing)
            if budget < 0:
                raise ValueError(f"Incomplete lesson profile batch for semester {term}")
            for code in missing:
                if len(codes) > 1:
                    part.update(await _profile_batch(session, term, [code], lessons))
                if code not in part:
                    absent.append(code)
        records.update(part)
    if requested and not records:
        raise ValueError(f"Public lesson profiles were empty for semester {term}")
    return TeachLessonInfosResponse(
        [records[code] for code in requested if code in records]
    ), absent


async def _public_index(session, term, cached, ended):
    url = f"{LESSON_LIST_URL}/{term}"

    async def read(allow_absence):
        try:
            response = TeachLessonListResponse.model_validate(await _get(session, url))
        except httpx.HTTPStatusError as error:
            if allow_absence and error.response.status_code in HISTORIC_NOT_FOUND:
                return None
            raise
        if not response.root:
            if allow_absence:
                return None
            raise ValueError(f"Public lesson list for semester {term} was empty")
        return response, _index(
            (row.code, row.id, row.course.id if row.course else None)
            for row in response.root
        )

    result = await read(ended) or await read(ended)
    if result is None:
        return None, cached
    response, lessons = result
    if len(lessons) * 100 < len(cached) * 95:
        _, confirmed = await read(False)
        removed = sorted(cached.keys() - lessons.keys())
        controls = sorted({removed[0], removed[-1]})
        if confirmed != lessons or await _profile_batch(
            session, term, controls, cached
        ):
            raise ValueError(
                f"Public lesson index for {term} shrank without corroboration"
            )
    return response, lessons


def _signature(lessons):
    # Keep the existing persisted signature compatible across code upgrades.
    digest = hashlib.sha256()
    for code, identity in sorted(lessons.items()):
        digest.update(
            json.dumps(
                [code, identity.id, identity.course_id], ensure_ascii=False
            ).encode()
        )
    return digest.hexdigest()


async def _refresh_term(
    session, store, term, cached, now, *, explicit, public_cached, curriculum
):
    key = f"catalog_semester_details_{term.id}"
    row = store.conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (key,)
    ).fetchone()
    previous = json.loads(row[0]) if row else {}
    metadata = dict(previous)
    if curriculum is not None:
        metadata["curriculum_signature"] = curriculum
    today = now.astimezone(TIME_ZONE).date()
    days_since_end = today - date.fromisoformat(term.end)
    ended = days_since_end > timedelta() and term.isLast is not True
    settled = ended and days_since_end > RECENT_TERM_WINDOW
    unavailable = previous.get("lesson_index_source") in ("cached", "unavailable")
    checked = _time(previous.get("lesson_index_checked_at"), now)
    recheck = bool(previous) and (
        checked is None
        or now - checked
        >= (RECENT_TERM_REFRESH_AGE if unavailable else SETTLED_TERM_REFRESH_AGE)
    )
    cached_signature = _signature(cached)
    changed_curriculum = (
        public_cached
        and curriculum is not None
        and curriculum != cached_signature
        and curriculum != previous.get("curriculum_signature")
    )
    recheck = recheck or changed_curriculum
    use_archive = (
        settled
        and not explicit
        and curriculum is not None
        and not public_cached
        and not recheck
        and not unavailable
    )

    def fresh(signature):
        stamp = _time(previous.get("refreshed_at"), now)
        max_age = SETTLED_TERM_REFRESH_AGE if settled else RECENT_TERM_REFRESH_AGE
        if not ended and date.fromisoformat(term.start) <= today:
            max_age = ACTIVE_REFRESH_AGE
        if previous.get("lesson_infos_unavailable") or previous.get(
            "unavailable_lesson_codes"
        ):
            max_age = min(max_age, RECENT_TERM_REFRESH_AGE)
        return (
            stamp is not None
            and now - stamp < max_age
            and previous.get("lesson_signature") == signature
        )

    if (
        settled
        and not explicit
        and not recheck
        and fresh(cached_signature)
        and (previous.get("lesson_index_source") != "curriculum" or use_archive)
    ):
        if metadata != previous:
            with store.conn:
                store.put_metadata({key: json.dumps(metadata, ensure_ascii=False)})
        return True

    profiles, absent, public = None, [], None
    lessons = cached
    if use_archive:
        try:
            profiles, absent = await _profiles(session, term.id, cached, complete=True)
        except LessonIndexMismatchError as error:
            LOGGER.info("Checking live index after a curriculum mismatch: %s", error)
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in HISTORIC_NOT_FOUND:
                raise
    archive_confirmed = profiles is not None
    if archive_confirmed:
        source = "curriculum"
    else:
        public, lessons = await _public_index(session, term.id, cached, ended)
        source = (
            "public" if public is not None else ("cached" if lessons else "unavailable")
        )
        if public is None:
            LOGGER.warning(
                "Official lesson index unavailable for semester %s (%s)",
                term.id,
                source,
            )
    signature = _signature(lessons)
    metadata.update(lesson_index_source=source, lesson_index_checked_at=now.isoformat())
    reused = fresh(signature)
    if not reused:
        if lessons and not archive_confirmed:
            profiles, absent = await _profiles(
                session,
                term.id,
                lessons,
                allow_absence=ended and not previous.get("lesson_profile_rows"),
            )
        count = len(profiles.root) if profiles is not None else 0
        if profiles is not None and count + len(absent) != len(lessons):
            raise ValueError(f"Lesson profile coverage did not reconcile for {term.id}")
        if count == 0 and previous.get("lesson_profile_rows", 0):
            raise ValueError(
                f"Existing lesson profiles became empty for semester {term.id}"
            )
        metadata.update(
            lesson_signature=signature,
            lesson_count=len(lessons),
            lesson_profile_rows=count,
            unavailable_lesson_codes=absent,
            lesson_infos_unavailable=profiles is None,
            refreshed_at=now.isoformat(),
        )

    with store.conn:
        if not archive_confirmed:
            _replace(store, CATALOG_PUBLIC_LESSONS, public, term.id)
        if not reused:
            _replace(store, CATALOG_LESSON_INFOS, profiles, term.id)
            if profiles is not None:
                for name, definition in PROFILE_INDEXES.items():
                    unique, fields = definition.split(":")
                    store.conn.execute(
                        f"CREATE {unique} INDEX IF NOT EXISTS {name} "
                        f"ON {CATALOG_LESSON_INFOS}({fields})"
                    )
        store.put_metadata({key: json.dumps(metadata, ensure_ascii=False)})
    return (
        not explicit or public is not None and not metadata["lesson_infos_unavailable"]
    )


async def refresh_catalog_semester_details(
    session: RequestSession,
    store: SQLiteModelStore,
    *,
    semester_ids: Collection[int] | None = None,
) -> None:
    now = datetime.now(UTC)
    today = now.astimezone(TIME_ZONE).date()
    response = TeachSemesterListResponse.model_validate(
        await _get(session, SEMESTER_LIST_URL)
    )
    terms, seen = {}, set()
    for term in response.root:
        if term.id is None or term.id <= 0 or term.id in seen:
            raise ValueError("Public semester index contains invalid or duplicate IDs")
        seen.add(term.id)
        if term.start is not None and term.end is not None:
            if date.fromisoformat(term.end) < date.fromisoformat(term.start):
                raise ValueError(f"Invalid semester dates for {term.id}")
            terms[term.id] = term
    if not response.root:
        raise ValueError("Public semester index was empty")
    with store.conn:
        _replace(store, CATALOG_PUBLIC_SEMESTERS, response)
    curriculum, verified = _stored_indexes(store, CATALOG_LESSONS)
    public, _ = _stored_indexes(store, CATALOG_PUBLIC_LESSONS)
    cached = {**curriculum, **public}
    metadata = dict(
        store.conn.execute(
            "SELECT key, value FROM metadata WHERE key GLOB 'curriculum_*'"
        )
    )
    covered = set(metadata.get("curriculum_successful_semester_ids", "").split(","))
    covered -= set(metadata.get("curriculum_unavailable_semester_ids", "").split(","))
    if metadata.get("curriculum_fetch_status") not in {
        "complete",
        "partial_sources_unavailable",
    }:
        verified.clear()
    explicit = None if semester_ids is None else set(semester_ids)
    if explicit is not None:
        if not explicit:
            raise ValueError("At least one explicit semester ID is required")
        if unknown := explicit - terms.keys():
            raise ValueError(
                f"Unknown or undated public semester IDs: {sorted(unknown)}"
            )
    selected = [
        term
        for term in terms.values()
        if (
            term.id in explicit
            if explicit is not None
            else (
                date.fromisoformat(term.start) <= today
                and (
                    term.isLast is True
                    or date.fromisoformat(term.end) >= today
                    or term.id in cached
                )
            )
        )
    ]
    if not selected:
        raise ValueError("Public semester index provided no active or cached semesters")
    failures = []
    for term in sorted(selected, key=lambda row: (row.start, row.id), reverse=True):
        signature = (
            _signature(curriculum[term.id])
            if term.id in verified and str(term.id) in covered
            else None
        )
        try:
            if await _refresh_term(
                session,
                store,
                term,
                cached.get(term.id, {}),
                now,
                explicit=semester_ids is not None,
                public_cached=term.id in public,
                curriculum=signature,
            ):
                continue
        except (httpx.HTTPError, ValueError) as error:
            LOGGER.warning(
                "Public semester %s refresh failed; last committed snapshot kept: %s",
                term.id,
                error,
            )
        failures.append(term.id)
    if failures:
        raise RuntimeError(f"Public semester details refresh incomplete: {failures}")
