"""Refresh the anonymous university course catalogue without class schedules."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from itertools import batched
from urllib.parse import urlencode

from .models.api.catalog_api_teach_course_infos import TeachCourseInfosResponse
from .models.api.catalog_api_teach_course_search import TeachCourseSearchResponse
from .sqlite_store import SQLiteModelStore
from .upstream_contracts import CATALOG_COURSE_INFOS, CATALOG_COURSE_SEARCH
from .utils.auth import RequestSession

COURSE_SEARCH_ENDPOINT = "https://catalog.ustc.edu.cn/api/teach/course/search"
COURSE_SEARCH_URL = f"{COURSE_SEARCH_ENDPOINT}?keyword="
COURSE_INFOS_URL = "https://catalog.ustc.edu.cn/api/teach/course/infos"
DETAIL_BATCH_SIZE = 100
DETAIL_REFRESH_AGE = timedelta(days=14)
DETAIL_REQUEST_INTERVAL_SECONDS = 0.15
ABSENT = f"{CATALOG_COURSE_INFOS}_absent"
OPTIONS = {"timeout": 60_000, "transient_retries": 1}
LOGGER = logging.getLogger(__name__)


async def _search(session, url=COURSE_SEARCH_URL):
    payload = await session.get_json(url, **OPTIONS)
    index = {}
    for row in TeachCourseSearchResponse.model_validate(payload).root:
        code = row.number
        if not code or code != code.strip():
            raise ValueError("The public course search returned an invalid course code")
        if index.get(code, row) != row:
            raise ValueError(f"Conflicting public course index records for {code!r}")
        index[code] = row
    return index


async def _confirm_shrink(session, previous, index):
    removed = sorted(previous.keys() - index.keys())
    checks = min(8, len(removed))
    for offset in range(checks):
        code = removed[offset * (len(removed) - 1) // max(1, checks - 1)]
        await asyncio.sleep(DETAIL_REQUEST_INTERVAL_SECONDS)
        query = urlencode({"keyword": code})
        if code in await _search(session, f"{COURSE_SEARCH_ENDPOINT}?{query}"):
            raise ValueError(f"Keyword search still returns missing {code!r}")
    await asyncio.sleep(DETAIL_REQUEST_INTERVAL_SECONDS)
    if await _search(session) != index:
        raise ValueError("The rechecked public index differs")


async def _details(session, requested, index):
    receipts, absent = [], set()

    async def fetch(codes):
        await asyncio.sleep(DETAIL_REQUEST_INTERVAL_SECONDS)
        payload = await session.post_json(
            COURSE_INFOS_URL, data={"codes": list(codes)}, **OPTIONS
        )
        checked = {}
        for row in TeachCourseInfosResponse.model_validate(payload).root:
            if row.code not in codes or row.code in checked:
                raise ValueError(
                    "Public course details returned an unexpected or repeated code: "
                    f"{row.code!r}"
                )
            checked[row.code] = row
        receipts.append(
            (len(codes), [checked[code] for code in codes if code in checked])
        )
        return [code for code in codes if code not in checked]

    for batch in batched(requested, DETAIL_BATCH_SIZE, strict=False):
        missing = await fetch(batch)
        if 1 < len(missing) < len(batch):
            missing = await fetch(missing)
        for code in sorted(missing, key=lambda code: not index[code].valid):
            if len(batch) > 1 and not await fetch([code]):
                continue
            if index[code].valid:
                raise ValueError(f"Active public course {code!r} has no detail")
            absent.add(code)
    return receipts, absent


def _fresh(timestamp, now):
    try:
        return (
            timedelta() <= now - datetime.fromisoformat(timestamp) < DETAIL_REFRESH_AGE
        )
    except (TypeError, ValueError):
        return False


def _delete_profiles(store, tables, discarded):
    if not discarded:
        return
    # Remove descendants before deleting the selected profiles, without touching
    # other courses that share their original fetch receipt.
    root = CATALOG_COURSE_INFOS
    ids = json.dumps(discarded)
    filters = {root: "store_id IN (SELECT value FROM json_each(?))"}
    for child in sorted(tables, key=len):
        parents = [parent for parent in filters if child.startswith(parent + "_")]
        if parents:
            parent = max(parents, key=len)
            filters[child] = (
                f'parent_store_id IN (SELECT store_id FROM "{parent}" '
                f"WHERE {filters[parent]})"
            )
    # Resolve child filters before deleting their ancestors.
    for table, scope in reversed(filters.items()):
        store.conn.execute(f'DELETE FROM "{table}" WHERE {scope}', (ids,))
    orphaned = [
        id_
        for (id_,) in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ? "
            f"AND id NOT IN (SELECT fetch_id FROM {root})",
            (root,),
        )
    ]
    store.delete_fetches(orphaned)


async def refresh_course_catalog(
    session: RequestSession, store: SQLiteModelStore
) -> None:
    db = store.conn
    tables = {
        name
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    searches, profiles = CATALOG_COURSE_SEARCH, CATALOG_COURSE_INFOS
    previous = {}
    if searches in tables:
        previous = dict(db.execute(f"SELECT number, payload_json FROM {searches}"))
    saved = {}
    if profiles in tables:
        rows = db.execute(
            f"SELECT d.code, d.store_id, f.fetched_at FROM {profiles} d "
            "JOIN upstream_fetches f ON f.id = d.fetch_id ORDER BY d.fetch_id"
        )
        saved = {code: (id_, fetched) for code, id_, fetched in rows}
    row = db.execute("SELECT value FROM metadata WHERE key = ?", (ABSENT,)).fetchone()
    absent = json.loads(row[0]) if row else {}

    index = await _search(session)
    if not index:
        raise ValueError("Public course search returned an empty index")
    if len(index) * 100 < len(previous) * 98:
        await _confirm_shrink(session, previous, index)
    now = datetime.now(UTC)
    encoded = {
        code: row.model_dump_json(by_alias=True, exclude_unset=True)
        for code, row in index.items()
    }
    requested = [
        code
        for code, summary in encoded.items()
        if summary != previous.get(code)
        or not _fresh(saved.get(code, (None, absent.get(code)))[1], now)
    ]
    LOGGER.info(
        "Refreshing %d profiles for %d catalogue codes", len(requested), len(index)
    )
    receipts, missing = await _details(session, requested, index)
    retained = index.keys() - set(requested)
    discards = [id_ for code, (id_, _) in saved.items() if code not in retained]
    absent = {code: date for code, date in absent.items() if code in retained}
    absent.update(dict.fromkeys(sorted(missing), now.isoformat()))
    count = len(saved) - len(discards) + sum(len(rows) for _, rows in receipts)
    if count + len(absent) != len(index):
        raise RuntimeError("Public course catalogue reconciliation failed")

    with db:
        store.register_response_model(
            table_name=profiles, response_model=TeachCourseInfosResponse
        )
        if encoded != previous:
            store.delete_source_fetches(searches)
            store.store_fetched_response(
                searches,
                COURSE_SEARCH_URL,
                TeachCourseSearchResponse(list(index.values())),
            )
        _delete_profiles(store, tables, discards)
        for size, rows in receipts:
            store.store_fetched_response(
                profiles,
                COURSE_INFOS_URL,
                TeachCourseInfosResponse(rows),
                method="POST",
                context={"requested": size, "returned": len(rows)},
            )
        store.put_metadata(
            {
                f"{searches}_count": len(index),
                f"{profiles}_count": count,
                f"{profiles}_absent_count": len(absent),
                ABSENT: json.dumps(absent, ensure_ascii=False),
            }
        )
