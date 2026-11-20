"""Refresh the anonymous university course catalogue without class schedules."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import batched
from urllib.parse import urlencode

from .models.api.catalog_api_teach_course_infos import (
    TeachCourseInfo,
    TeachCourseInfosResponse,
)
from .models.api.catalog_api_teach_course_search import (
    TeachCourseSearchItem,
    TeachCourseSearchResponse,
)
from .sqlite_store import SQLiteModelStore
from .upstream_contracts import CATALOG_COURSE_INFOS, CATALOG_COURSE_SEARCH
from .utils.auth import RequestSession

COURSE_SEARCH_ENDPOINT = "https://catalog.ustc.edu.cn/api/teach/course/search"
COURSE_SEARCH_URL = f"{COURSE_SEARCH_ENDPOINT}?keyword="
COURSE_INFOS_URL = "https://catalog.ustc.edu.cn/api/teach/course/infos"
DETAIL_BATCH_SIZE = 100
DETAIL_REFRESH_AGE = timedelta(days=14)
DETAIL_REQUEST_INTERVAL_SECONDS = 0.15
_SHRINK_PROBE_COUNT = 8
_ABSENT_METADATA = f"{CATALOG_COURSE_INFOS}_absent"

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CachedDetail:
    store_id: int
    fetched_at: str


def _recent(value: str | None, threshold: datetime) -> bool:
    if value is None:
        return False
    try:
        timestamp = datetime.fromisoformat(value)
        return timestamp.tzinfo is not None and timestamp > threshold
    except ValueError:
        return False


def _index_records(payload: object) -> dict[str, TeachCourseSearchItem]:
    index: dict[str, TeachCourseSearchItem] = {}
    for item in TeachCourseSearchResponse.model_validate(payload).root:
        code = item.number
        if not code.strip() or code != code.strip():
            raise ValueError("The public course search returned an invalid course code")
        previous = index.get(code)
        if previous is not None and previous != item:
            raise ValueError(f"Conflicting public course index records for {code!r}")
        index[code] = item
    return index


async def _search_catalog(
    session: RequestSession, url: str
) -> dict[str, TeachCourseSearchItem]:
    payload = await session.get_json(url, timeout=60_000, transient_retries=1)
    return _index_records(payload)


async def _confirm_smaller_index(
    session: RequestSession,
    previous_index: Mapping[str, str],
    candidate: dict[str, TeachCourseSearchItem],
) -> None:
    """Check a spread of omitted codes before replacing a smaller catalogue."""
    removed = sorted(previous_index.keys() - candidate.keys())
    sample_size = min(_SHRINK_PROBE_COUNT, len(removed))
    message = (
        f"Public course index shrank from {len(previous_index)} cached codes "
        f"to {len(candidate)}. The existing snapshot was kept. "
    )
    for sample in range(sample_size):
        position = sample * (len(removed) - 1) // max(1, sample_size - 1)
        code = removed[position]
        await asyncio.sleep(DETAIL_REQUEST_INTERVAL_SECONDS)
        query = urlencode({"keyword": code})
        matches = await _search_catalog(session, f"{COURSE_SEARCH_ENDPOINT}?{query}")
        if code in matches:
            raise ValueError(
                message + f"Keyword search still returns missing {code!r}."
            )

    await asyncio.sleep(DETAIL_REQUEST_INTERVAL_SECONDS)
    if await _search_catalog(session, COURSE_SEARCH_URL) != candidate:
        raise ValueError(message + "The rechecked public index differs.")
    LOGGER.info(
        "Confirmed smaller public course index (%d → %d) with %d code checks",
        len(previous_index),
        len(candidate),
        sample_size,
    )


def _existing_courses(
    store: SQLiteModelStore,
) -> tuple[dict[str, str], dict[str, _CachedDetail], dict[str, str]]:
    table_names = {
        name
        for (name,) in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    summaries = (
        dict(
            store.conn.execute(
                f"SELECT number, payload_json FROM {CATALOG_COURSE_SEARCH}"
            )
        )
        if CATALOG_COURSE_SEARCH in table_names
        else {}
    )
    details = (
        {
            code: _CachedDetail(store_id, fetched_at)
            for code, store_id, fetched_at in store.conn.execute(
                f"SELECT d.code, d.store_id, f.fetched_at "
                f"FROM {CATALOG_COURSE_INFOS} d "
                "JOIN upstream_fetches f ON f.id = d.fetch_id "
                "ORDER BY d.fetch_id"
            )
        }
        if CATALOG_COURSE_INFOS in table_names
        else {}
    )
    absent_raw = store.conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (_ABSENT_METADATA,)
    ).fetchone()
    absent = json.loads(absent_raw[0]) if absent_raw else {}
    return summaries, details, absent


async def _detail_records(
    session: RequestSession, codes: Iterable[str]
) -> dict[str, TeachCourseInfo]:
    requested = list(codes)
    await asyncio.sleep(DETAIL_REQUEST_INTERVAL_SECONDS)
    payload = await session.post_json(
        COURSE_INFOS_URL,
        data={"codes": requested},
        timeout=60_000,
        transient_retries=1,
    )
    wanted = set(requested)
    records: dict[str, TeachCourseInfo] = {}
    for record in TeachCourseInfosResponse.model_validate(payload).root:
        if record.code not in wanted or record.code in records:
            raise ValueError(
                f"Public course details returned an unexpected or repeated code "
                f"{record.code!r}"
            )
        records[record.code] = record
    return records


async def _refresh_details(
    session: RequestSession,
    requested: list[str],
    index: Mapping[str, TeachCourseSearchItem],
) -> tuple[list[tuple[list[str], dict[str, TeachCourseInfo]]], set[str]]:
    fetched: list[tuple[list[str], dict[str, TeachCourseInfo]]] = []
    absent: set[str] = set()
    for batch in batched(requested, DETAIL_BATCH_SIZE, strict=False):
        records = await _detail_records(session, batch)
        fetched.append((list(batch), records))
        missing = [code for code in batch if code not in records]
        if 1 < len(missing) < len(batch):
            recovered = await _detail_records(session, missing)
            fetched.append((missing, recovered))
            missing = [code for code in missing if code not in recovered]
        # Probe omissions individually before classifying them as unavailable.
        # Probe active courses first to stop quickly if the source is incomplete.
        for code in sorted(missing, key=lambda code: not index[code].valid):
            if len(batch) > 1:
                singleton = await _detail_records(session, [code])
                fetched.append(([code], singleton))
                if singleton:
                    continue
            if index[code].valid:
                raise ValueError(f"Active public course {code!r} has no detail")
            absent.add(code)
    return fetched, absent


def _source_fetch_ids(store: SQLiteModelStore, source: str) -> list[int]:
    return [
        fetch_id
        for (fetch_id,) in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ?", (source,)
        )
    ]


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _remove_detail_rows(store: SQLiteModelStore, ids: Collection[int]) -> None:
    if not ids:
        return
    descendants = defaultdict(list)
    tables = [CATALOG_COURSE_INFOS]
    for (name,) in store.conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name GLOB ?
        ORDER BY length(name), name
        """,
        (f"{CATALOG_COURSE_INFOS}_*",),
    ):
        parent = max(
            (table for table in tables if name.startswith(table + "_")), key=len
        )
        descendants[parent].append(name)
        tables.append(name)

    def remove(table: str, store_ids: Collection[int]) -> None:
        quoted = _quote_identifier(table)
        for chunk in batched(store_ids, 500, strict=False):
            placeholders = ",".join("?" for _ in chunk)
            for child in descendants[table]:
                child_ids = [
                    store_id
                    for (store_id,) in store.conn.execute(
                        f"SELECT store_id FROM {_quote_identifier(child)} "
                        f"WHERE parent_store_id IN ({placeholders})",
                        chunk,
                    )
                ]
                remove(child, child_ids)
            store.conn.execute(
                f"DELETE FROM {quoted} WHERE store_id IN ({placeholders})", chunk
            )

    remove(CATALOG_COURSE_INFOS, ids)
    # Pruning is separate from deleting models: a previous batch can still
    # own retained rows, which preserve their original fetch time.
    orphaned_fetches = [
        fetch_id
        for (fetch_id,) in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ? "
            f"AND id NOT IN (SELECT DISTINCT fetch_id FROM {CATALOG_COURSE_INFOS})",
            (CATALOG_COURSE_INFOS,),
        )
    ]
    store.delete_fetches(orphaned_fetches)


def _persist(
    store: SQLiteModelStore,
    index: dict[str, TeachCourseSearchItem],
    fetched: list[tuple[list[str], dict[str, TeachCourseInfo]]],
    absent: dict[str, str],
    *,
    index_changed: bool,
    retained_count: int,
    discard_ids: list[int],
) -> None:
    with store.conn:
        store.register_response_model(
            table_name=CATALOG_COURSE_INFOS,
            response_model=TeachCourseInfosResponse,
        )
        summary_count = len(index)
        if index_changed:
            store.delete_fetches(_source_fetch_ids(store, CATALOG_COURSE_SEARCH))
            search_fetch = store.record_fetch(
                source=CATALOG_COURSE_SEARCH, method="GET", url=COURSE_SEARCH_URL
            )
            summary_count = store.store_response(
                table_name=CATALOG_COURSE_SEARCH,
                response=TeachCourseSearchResponse(list(index.values())),
                fetch_id=search_fetch,
            )

        _remove_detail_rows(store, discard_ids)
        details_count = retained_count
        for codes, records in fetched:
            fetch_id = store.record_fetch(
                source=CATALOG_COURSE_INFOS,
                method="POST",
                url=COURSE_INFOS_URL,
                context={
                    "requested": len(codes),
                    "returned": len(records),
                },
            )
            if records:
                count = store.store_response(
                    table_name=CATALOG_COURSE_INFOS,
                    response=TeachCourseInfosResponse(
                        [records[code] for code in codes if code in records]
                    ),
                    fetch_id=fetch_id,
                )
                details_count += count
        if summary_count != details_count + len(absent):
            raise RuntimeError("Public course catalogue reconciliation failed")
        store.put_metadata(
            {
                f"{CATALOG_COURSE_SEARCH}_count": summary_count,
                f"{CATALOG_COURSE_INFOS}_count": details_count,
                f"{CATALOG_COURSE_INFOS}_absent_count": len(absent),
                _ABSENT_METADATA: json.dumps(absent, ensure_ascii=False),
            }
        )


async def refresh_course_catalog(
    session: RequestSession, store: SQLiteModelStore
) -> None:
    previous_index, previous_details, previous_absent = _existing_courses(store)
    index = await _search_catalog(session, COURSE_SEARCH_URL)
    if not index:
        raise ValueError("Public course search returned an empty index")
    if len(index) * 100 < len(previous_index) * 98:
        await _confirm_smaller_index(session, previous_index, index)

    now = datetime.now(UTC)
    threshold = now - DETAIL_REFRESH_AGE
    encoded_index = {
        code: item.model_dump_json(by_alias=True, exclude_unset=True)
        for code, item in index.items()
    }
    requested = [
        code
        for code, encoded in encoded_index.items()
        if encoded != previous_index.get(code)
        or not _recent(
            previous_details[code].fetched_at
            if code in previous_details
            else previous_absent.get(code),
            threshold,
        )
    ]
    LOGGER.info(
        "Public course index has %d distinct codes, refreshing %d profiles",
        len(index),
        len(requested),
    )
    fetched, newly_absent = await _refresh_details(session, requested, index)
    requested_codes = set(requested)
    discard_ids = [
        detail.store_id
        for code, detail in previous_details.items()
        if code not in index or code in requested_codes
    ]
    absent = {
        code: at
        for code, at in previous_absent.items()
        if code in index and code not in requested_codes
    }
    absent.update(dict.fromkeys(sorted(newly_absent), now.isoformat()))
    _persist(
        store,
        index,
        fetched,
        absent,
        index_changed=encoded_index != previous_index,
        retained_count=len(previous_details) - len(discard_ids),
        discard_ids=discard_ids,
    )
    LOGGER.info(
        "Public course catalogue saved %d profiles (%d confirmed unavailable)",
        len(index) - len(absent),
        len(absent),
    )
