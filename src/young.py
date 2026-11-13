from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from .sqlite_store import SNAPSHOT_FILENAME, SQLiteModelStore
from .utils.auth import RequestSession, USTCSession
from .utils.tools import BUILD_DIR

YOUNG_API_BASE = "https://young.ustc.edu.cn/login/wisdom-group-learning-bg"
YOUNG_SIGNUP_URL = (
    "https://young.ustc.edu.cn/login/sc-wisdom-group-learning/myproject/SignUp"
)
YOUNG_TOKEN_STORAGE_KEY = "pro__Access-Token-zsxc-base"
YOUNG_PAGE_SIZE = 3000
YOUNG_ACTIVE_ENDPOINT = "/mobile/item/enrolmentList"
YOUNG_ENDED_ENDPOINT = "/mobile/item/endList"
YOUNG_ACTIVE_SOURCE = "young_mobile_item_enrolment_list"
YOUNG_ENDED_SOURCE = "young_mobile_item_end_list"
YOUNG_NAMES_SOURCE = "young_referenced_organization_names"
YOUNG_NAMES_SCOPE = "session-visible-tree/referenced-young-records-only"
YOUNG_TREE_ENDPOINT = "/sysdepart/sysDepart/queryTreeList"
YOUNG_TREE_MAX_BYTES = 16 * 1024 * 1024

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class YoungReferencedNames:
    names: dict[str, str]
    response_sha256: str
    observed_at: str


def _young_referenced_ids(
    records: list[dict[str, Any]],
) -> tuple[set[str], bool]:
    ids: set[str] = set()
    complete = True
    for record in records:
        for field in ("organizer", "sponsor"):
            value = record.get(field)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        # Actual array members are opaque, including commas.
                        ids.add(item)
                    else:
                        complete = False
            elif isinstance(value, str):
                if value == "":
                    continue
                for item in value.split(","):
                    token = item.strip()
                    if token:
                        ids.add(token)
                    else:
                        complete = False
            else:
                complete = False
    return ids, complete


def _referenced_tree_names(payload: object, references: set[str]) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Young organization tree did not succeed")
    roots = payload.get("result")
    if not isinstance(roots, list) or not roots:
        raise ValueError("Young organization tree is empty or malformed")
    stack = [(node, None, 1) for node in roots]
    seen: set[str] = set()
    names: dict[str, str] = {}
    while stack:
        node, parent, depth = stack.pop()
        if not isinstance(node, dict) or depth > 64 or len(seen) >= 20_000:
            raise ValueError("Young organization tree exceeds its shape bounds")
        source_id = node.get("id")
        title = node.get("title")
        if (
            not isinstance(source_id, str)
            or not source_id.strip()
            or source_id in seen
            or node.get("key") != source_id
            or node.get("value") != source_id
            or not isinstance(title, str)
            or not title.strip()
            or node.get("departName") != title
            or not isinstance(node.get("parentId"), str)
            or (parent is not None and node["parentId"] != parent)
        ):
            raise ValueError("Young organization tree has inconsistent identities")
        seen.add(source_id)
        if source_id in references:
            names[source_id] = title
        children = node.get("children")
        if children is None:
            children = []
        if not isinstance(children, list):
            raise ValueError("Young organization tree has malformed children")
        stack.extend((child, source_id, depth + 1) for child in children)
    if any(node["parentId"] in seen for node in roots):
        raise ValueError("Young organization tree has an internal root parent")
    return names


async def _fetch_young_referenced_names(
    session: RequestSession, references: set[str]
) -> YoungReferencedNames:
    if session.page is None:
        raise RuntimeError("Young organization names require the existing browser")
    page = session.page
    await page.wait_for_function(
        """() => {
            const vm = document.getElementById('app')?.__vue__;
            return !!(vm && vm.$http && vm.$http === vm.axios &&
                vm.$http.defaults.baseURL ===
                'https://young.ustc.edu.cn/login/wisdom-group-learning-bg/');
        }""",
        timeout=session.timeout_ms,
    )

    def is_tree_response(response: Any) -> bool:
        url = urlsplit(response.url)
        return (
            url.scheme == "https"
            and url.netloc == "young.ustc.edu.cn"
            and url.path == "/login/wisdom-group-learning-bg" + YOUNG_TREE_ENDPOINT
        )

    async with page.expect_response(
        is_tree_response, timeout=session.timeout_ms
    ) as pending:
        valid = await page.evaluate(
            """async () => {
                const vm = document.getElementById('app').__vue__;
                const data = await vm.$http.get('/sysdepart/sysDepart/queryTreeList');
                return data?.success === true && Array.isArray(data.result);
            }""",
            isolated_context=False,
        )
    response = await pending.value
    if not valid or response.status != 200:
        raise ValueError("Young organization tree did not return HTTP success")
    raw = await response.body()
    if len(raw) > YOUNG_TREE_MAX_BYTES:
        raise ValueError("Young organization tree exceeds the parse limit")
    # Only this allowlisted projection leaves memory, never the raw tree or parents.
    return YoungReferencedNames(
        names=_referenced_tree_names(json.loads(raw), references),
        response_sha256=hashlib.sha256(raw).hexdigest(),
        observed_at=datetime.now(UTC).isoformat(),
    )


def _refresh_young_referenced_names(
    store: SQLiteModelStore,
    references: set[str],
    *,
    complete: bool,
    observation: YoungReferencedNames | None,
    status: str,
) -> None:
    if not complete and not references and observation is None:
        store.put_metadata(
            {
                "young_names_last_attempt": "references_unknown",
                "young_names_reference_complete": 0,
                "young_names_reference_count": 0,
            }
        )
        logger.info(
            "Young referenced names result: referenceComplete=%s referencedCount=%d "
            "freshMatchedCount=%d status=%s",
            False,
            0,
            0,
            "references_unknown",
        )
        return
    store.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS young_referenced_organization_names (
          id TEXT PRIMARY KEY, title TEXT NOT NULL,
          fetch_id INTEGER NOT NULL REFERENCES upstream_fetches(id)
        )
        """
    )
    # Complete reference scope is enforced outside the optional fresh-observation
    # savepoint. Partial scope can add positives, never prune the previous cohort.
    if complete:
        previous = store.conn.execute(
            "SELECT id FROM young_referenced_organization_names"
        ).fetchall()
        store.conn.executemany(
            "DELETE FROM young_referenced_organization_names WHERE id = ?",
            ((row[0],) for row in previous if row[0] not in references),
        )
    cohort = json.dumps(sorted(references), ensure_ascii=False, separators=(",", ":"))
    store.put_metadata(
        {
            "young_names_reference_sha256": hashlib.sha256(cohort.encode()).hexdigest(),
            "young_names_reference_count": len(references),
            "young_names_reference_complete": int(complete),
        }
    )
    fresh_matched = 0
    if observation is not None:
        store.conn.execute("SAVEPOINT young_referenced_names_refresh")
        try:
            if not observation.names.keys() <= references:
                raise ValueError("Young organization names exceed the reference scope")
            fetch_id = store.record_fetch(
                source=YOUNG_NAMES_SOURCE,
                method="GET",
                url=YOUNG_API_BASE + YOUNG_TREE_ENDPOINT,
                context={
                    "response_sha256": observation.response_sha256,
                    "observed_at": observation.observed_at,
                    "scope": YOUNG_NAMES_SCOPE,
                },
            )
            store.conn.executemany(
                """
                INSERT INTO young_referenced_organization_names(id,title,fetch_id)
                VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET
                  title=excluded.title, fetch_id=excluded.fetch_id
                """,
                ((key, title, fetch_id) for key, title in observation.names.items()),
            )
            # Unmatched but still-referenced cached rows keep their original
            # title/fetch. Only genuinely orphaned directory fetches are removed.
            orphan_fetches = [
                row[0]
                for row in store.conn.execute(
                    """
                    SELECT id FROM upstream_fetches WHERE source = ? AND id != ?
                      AND id NOT IN (
                        SELECT fetch_id FROM young_referenced_organization_names
                      )
                    """,
                    (YOUNG_NAMES_SOURCE, fetch_id),
                )
            ]
            store.delete_fetches(orphan_fetches)
            store.put_metadata(
                {
                    "young_names_response_sha256": observation.response_sha256,
                    "young_names_observed_at": observation.observed_at,
                    "young_names_scope": YOUNG_NAMES_SCOPE,
                }
            )
        except Exception as error:
            store.conn.execute("ROLLBACK TO young_referenced_names_refresh")
            store.conn.execute("RELEASE young_referenced_names_refresh")
            if isinstance(error, sqlite3.Error):
                raise
            status = "write_failed"
            logger.warning("Young referenced organization names: write_failed")
        else:
            store.conn.execute("RELEASE young_referenced_names_refresh")
            fresh_matched = len(observation.names)
            status = (
                "ok"
                if complete and references <= observation.names.keys()
                else "partial"
            )
    store.put_metadata({"young_names_last_attempt": status})
    logger.info(
        "Young referenced names result: referenceComplete=%s referencedCount=%d "
        "freshMatchedCount=%d status=%s",
        complete,
        len(references),
        fresh_matched,
        status,
    )


def _young_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise TypeError("Young endpoint returned no result object")
    return result


def _young_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = _young_result(payload)
    records = result.get("records")
    if records is None and result.get("total") == 0:
        return []
    if not isinstance(records, list):
        raise TypeError("Young endpoint returned non-list records")
    if any(not isinstance(record, dict) for record in records):
        raise TypeError("Young endpoint returned a malformed record")
    return records


def _young_total(payload: dict[str, Any]) -> int:
    result = _young_result(payload)
    total = result.get("total")
    if isinstance(total, bool) or not isinstance(total, int):
        raise TypeError("Young endpoint returned an invalid total")
    if total < 0:
        raise ValueError("Young endpoint returned a negative total")
    return total


def _delete_young_source(store: SQLiteModelStore, source: str) -> None:
    fetch_ids = [
        row[0]
        for row in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ?",
            (source,),
        )
    ]
    store.delete_fetches(fetch_ids)


def _validate_young_record_ids(records: list[dict[str, Any]], *, endpoint: str) -> None:
    seen_ids: set[str] = set()
    for record in records:
        event_id = record.get("id")
        if isinstance(event_id, bool) or not isinstance(event_id, (int, str)):
            raise TypeError(f"Young endpoint {endpoint} returned a record without id")
        normalized_id = str(event_id).strip()
        if not normalized_id:
            raise ValueError(f"Young endpoint {endpoint} returned an empty id")
        if normalized_id in seen_ids:
            raise ValueError(
                f"Young endpoint {endpoint} returned duplicate event id {normalized_id}"
            )
        seen_ids.add(normalized_id)


def _store_young_event_payload(
    store: SQLiteModelStore,
    *,
    source: str,
    endpoint: str,
    payload: dict[str, Any],
    list_type: str,
    page_size: int,
) -> int:
    records = _young_records(payload)
    total = _young_total(payload)
    _validate_young_record_ids(records, endpoint=endpoint)
    if len(records) != total:
        raise ValueError(
            f"Young endpoint {endpoint} returned {len(records)} records "
            f"for total {total}"
        )
    fetch_id = store.record_fetch(
        source=source,
        method="GET",
        url=_young_api_url(endpoint, {"pageNo": 1, "pageSize": page_size}),
        context={"list_type": list_type, "page_size": page_size},
    )
    store.store_json_response(
        table_name=source,
        payload=payload,
        fetch_id=fetch_id,
        context={"list_type": list_type},
    )
    store.put_metadata(
        {
            f"{source}_record_count": len(records),
            f"{source}_total": total,
        }
    )
    return len(records)


def _young_api_url(endpoint: str, params: dict[str, int | str]) -> str:
    return f"{YOUNG_API_BASE}{endpoint}?{urlencode(params)}"


def _access_token_from_storage(raw: str | None) -> str:
    if not raw:
        raise RuntimeError(f"Missing Young token storage key {YOUNG_TOKEN_STORAGE_KEY}")

    parsed = json.loads(raw)
    token = parsed.get("value") if isinstance(parsed, dict) else parsed
    if not isinstance(token, str) or not token:
        raise RuntimeError("Young token storage did not contain a token string")
    return token


async def _prepare_young_session(session: RequestSession) -> None:
    if session.page is None:
        raise RuntimeError("Young scraping requires a browser-backed session")

    await session.page.goto(
        YOUNG_SIGNUP_URL,
        wait_until="networkidle",
        timeout=session.timeout_ms,
    )
    raw_token = await session.page.evaluate(
        f"localStorage.getItem({YOUNG_TOKEN_STORAGE_KEY!r})"
    )
    token = _access_token_from_storage(raw_token)
    await session.sync_cookies_from_page()
    session.client.headers["x-access-token"] = token
    session.client.headers["referer"] = YOUNG_SIGNUP_URL


async def _fetch_young_event_page(
    session: Any, *, endpoint: str, page_no: int, page_size: int
) -> dict[str, Any]:
    payload = await session.get_json(
        _young_api_url(endpoint, {"pageNo": page_no, "pageSize": page_size})
    )
    if not isinstance(payload, dict):
        raise TypeError(f"Young endpoint {endpoint} returned non-object payload")
    if payload.get("success") is not True:
        message = payload.get("message")
        raise RuntimeError(f"Young endpoint {endpoint} failed: {message}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Young endpoint {endpoint} returned no result object")
    _young_total(payload)
    _young_records(payload)
    return payload


async def _fetch_young_event_list(
    session: Any, *, endpoint: str, page_size: int = 3000
) -> dict[str, Any]:
    if page_size < 1:
        raise ValueError("Young page size must be positive")

    records: list[dict[str, Any]] = []
    first_payload: dict[str, Any] | None = None
    expected_total: int | None = None
    seen_ids: set[str] = set()
    page_no = 1

    while True:
        payload = await _fetch_young_event_page(
            session,
            endpoint=endpoint,
            page_no=page_no,
            page_size=page_size,
        )
        if first_payload is None:
            first_payload = payload

        page_records = _young_records(payload)
        page_total = _young_total(payload)
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise ValueError(f"Young endpoint {endpoint} changed total between pages")
        if len(page_records) > page_size:
            raise ValueError(f"Young endpoint {endpoint} returned an oversized page")

        _validate_young_record_ids(page_records, endpoint=endpoint)
        for record in page_records:
            normalized_id = str(record["id"]).strip()
            if normalized_id in seen_ids:
                raise ValueError(
                    f"Young endpoint {endpoint} repeated event id {normalized_id}"
                )
            seen_ids.add(normalized_id)

        records.extend(page_records)
        if expected_total == len(records):
            break
        if not page_records:
            raise ValueError(
                f"Young endpoint {endpoint} ended before returning its total"
            )
        if len(records) > expected_total:
            raise ValueError(
                f"Young endpoint {endpoint} returned more records than its total"
            )
        page_no += 1

    if first_payload is None or expected_total is None:
        raise RuntimeError(f"Young endpoint {endpoint} returned no pages")
    if len(records) != expected_total:
        raise ValueError(
            f"Young endpoint {endpoint} returned an incomplete record list"
        )
    _young_result(first_payload)["records"] = records
    return first_payload


async def make_young_events() -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = BUILD_DIR / SNAPSHOT_FILENAME
    reuse_snapshot = snapshot_path.exists()

    with tempfile.TemporaryDirectory(
        dir=BUILD_DIR, prefix="young-refresh-"
    ) as temporary_dir:
        staged_snapshot_path = Path(temporary_dir) / SNAPSHOT_FILENAME
        if reuse_snapshot:
            shutil.copy2(snapshot_path, staged_snapshot_path)

        store = SQLiteModelStore(staged_snapshot_path, reset=not reuse_snapshot)
        try:
            async with USTCSession(after_login_services=False) as session:
                await _prepare_young_session(session)
                active_payload = await _fetch_young_event_list(
                    session,
                    endpoint=YOUNG_ACTIVE_ENDPOINT,
                    page_size=YOUNG_PAGE_SIZE,
                )
                ended_payload = await _fetch_young_event_list(
                    session,
                    endpoint=YOUNG_ENDED_ENDPOINT,
                    page_size=YOUNG_PAGE_SIZE,
                )
                references, references_complete = _young_referenced_ids(
                    _young_records(active_payload) + _young_records(ended_payload)
                )
                names = None
                names_status = "no_references"
                if references:
                    try:
                        async with asyncio.timeout(
                            min(session.timeout_ms, 60_000) / 1_000
                        ):
                            names = await _fetch_young_referenced_names(
                                session, references
                            )
                    except Exception:
                        names_status = "fetch_failed"
                        logger.warning(
                            "Young referenced organization names: fetch_failed"
                        )

            _delete_young_source(store, YOUNG_ACTIVE_SOURCE)
            active_count = _store_young_event_payload(
                store,
                source=YOUNG_ACTIVE_SOURCE,
                endpoint=YOUNG_ACTIVE_ENDPOINT,
                payload=active_payload,
                list_type="active",
                page_size=YOUNG_PAGE_SIZE,
            )
            _delete_young_source(store, YOUNG_ENDED_SOURCE)
            ended_count = _store_young_event_payload(
                store,
                source=YOUNG_ENDED_SOURCE,
                endpoint=YOUNG_ENDED_ENDPOINT,
                payload=ended_payload,
                list_type="ended",
                page_size=YOUNG_PAGE_SIZE,
            )

            # Keep Young freshness independent from the shared snapshot
            # generated_at, which unrelated builders may update later.
            young_synced_at = datetime.now(UTC).isoformat()
            store.put_metadata(
                {
                    "young_events_mode": "full",
                    "young_events_synced_at": young_synced_at,
                    "young_active_record_count": active_count,
                    "young_active_total": _young_total(active_payload),
                    "young_ended_record_count": ended_count,
                    "young_ended_total": _young_total(ended_payload),
                }
            )
            _refresh_young_referenced_names(
                store,
                references,
                complete=references_complete,
                observation=names,
                status=names_status,
            )
            logger.info(
                "Stored %s active Young event(s) and %s ended event(s)",
                active_count,
                ended_count,
            )
        finally:
            store.close()

        # The previous usable snapshot is untouched until both lists have been
        # fetched, validated, and committed to the staging database.
        os.replace(staged_snapshot_path, snapshot_path)
