from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

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
YOUNG_ENDED_PROBE_SOURCE = "young_mobile_item_end_list_probe"

logger = logging.getLogger(__name__)


def _should_refresh_ended_events(*, cached_count: int, upstream_total: int) -> bool:
    return cached_count != upstream_total


def _young_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    return result


def _young_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = _young_result(payload).get("records") or []
    return [record for record in records if isinstance(record, dict)]


def _young_total(payload: dict[str, Any]) -> int:
    result = _young_result(payload)
    return int(result.get("total") or len(_young_records(payload)))


def _cached_young_event_count(store: SQLiteModelStore, source: str) -> int:
    table_name = f"{source}_result_records"
    table_exists = store.conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    if table_exists is None:
        return 0

    row = store.conn.execute(
        f"""
        SELECT COUNT(*)
        FROM "{table_name}" records
        JOIN upstream_fetches fetches ON fetches.id = records.fetch_id
        WHERE fetches.source = ? AND fetches.ok = 1
        """,
        (source,),
    ).fetchone()
    return int(row[0]) if row else 0


def _delete_young_source(store: SQLiteModelStore, source: str) -> None:
    fetch_ids = [
        row[0]
        for row in store.conn.execute(
            "SELECT id FROM upstream_fetches WHERE source = ?",
            (source,),
        )
    ]
    store.delete_fetches(fetch_ids)


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
    return payload


async def _fetch_young_event_list(
    session: Any, *, endpoint: str, page_size: int = 3000
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    first_payload: dict[str, Any] | None = None
    page_no = 1

    while True:
        payload = await _fetch_young_event_page(
            session,
            endpoint=endpoint,
            page_no=page_no,
            page_size=page_size,
        )
        result = _young_result(payload)

        if first_payload is None:
            first_payload = payload

        page_records = result.get("records") or []
        if not isinstance(page_records, list):
            raise TypeError(f"Young endpoint {endpoint} returned non-list records")
        records.extend(record for record in page_records if isinstance(record, dict))

        total = int(result.get("total") or len(records))
        if len(records) >= total or not page_records:
            break
        page_no += 1

    if first_payload is None:
        raise RuntimeError(f"Young endpoint {endpoint} returned no pages")
    first_payload["result"]["records"] = records
    return first_payload


def _record_young_probe(
    store: SQLiteModelStore, *, endpoint: str, payload: dict[str, Any], page_size: int
) -> int:
    _delete_young_source(store, YOUNG_ENDED_PROBE_SOURCE)
    total = _young_total(payload)
    fetch_id = store.record_fetch(
        source=YOUNG_ENDED_PROBE_SOURCE,
        method="GET",
        url=_young_api_url(endpoint, {"pageNo": 1, "pageSize": page_size}),
        context={"page_size": page_size},
    )
    store.put_metadata(
        {
            "young_ended_probe_total": total,
            "young_ended_probe_fetch_id": fetch_id,
        }
    )
    return total


async def make_young_events() -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = BUILD_DIR / SNAPSHOT_FILENAME
    reuse_snapshot = snapshot_path.exists()
    store = SQLiteModelStore(snapshot_path, reset=not reuse_snapshot)

    try:
        async with USTCSession(after_login_services=False) as session:
            await _prepare_young_session(session)

            _delete_young_source(store, YOUNG_ACTIVE_SOURCE)
            active_payload = await _fetch_young_event_list(
                session,
                endpoint=YOUNG_ACTIVE_ENDPOINT,
                page_size=YOUNG_PAGE_SIZE,
            )
            active_count = _store_young_event_payload(
                store,
                source=YOUNG_ACTIVE_SOURCE,
                endpoint=YOUNG_ACTIVE_ENDPOINT,
                payload=active_payload,
                list_type="active",
                page_size=YOUNG_PAGE_SIZE,
            )

            ended_probe = await _fetch_young_event_page(
                session,
                endpoint=YOUNG_ENDED_ENDPOINT,
                page_no=1,
                page_size=1,
            )
            ended_total = _record_young_probe(
                store,
                endpoint=YOUNG_ENDED_ENDPOINT,
                payload=ended_probe,
                page_size=1,
            )
            cached_ended_count = (
                _cached_young_event_count(store, YOUNG_ENDED_SOURCE)
                if reuse_snapshot
                else 0
            )
            refresh_ended = _should_refresh_ended_events(
                cached_count=cached_ended_count,
                upstream_total=ended_total,
            )
            if refresh_ended:
                _delete_young_source(store, YOUNG_ENDED_SOURCE)
                ended_payload = await _fetch_young_event_list(
                    session,
                    endpoint=YOUNG_ENDED_ENDPOINT,
                    page_size=max(YOUNG_PAGE_SIZE, ended_total),
                )
                ended_count = _store_young_event_payload(
                    store,
                    source=YOUNG_ENDED_SOURCE,
                    endpoint=YOUNG_ENDED_ENDPOINT,
                    payload=ended_payload,
                    list_type="ended",
                    page_size=max(YOUNG_PAGE_SIZE, ended_total),
                )
            else:
                ended_count = cached_ended_count

            store.put_metadata(
                {
                    "young_events_mode": "incremental" if reuse_snapshot else "all",
                    "young_events_cache_source": "previous_artifact"
                    if reuse_snapshot
                    else "none",
                    "young_active_refreshed": 1,
                    "young_active_record_count": active_count,
                    "young_ended_refreshed": int(refresh_ended),
                    "young_ended_cached_record_count": cached_ended_count,
                    "young_ended_record_count": ended_count,
                    "young_ended_total": ended_total,
                }
            )
            logger.info(
                "Stored %s active Young event(s); %s ended event(s), refreshed=%s",
                active_count,
                ended_count,
                refresh_ended,
            )
    finally:
        store.close()
