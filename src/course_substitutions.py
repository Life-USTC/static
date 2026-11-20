"""Refresh the official course substitution pool without merging directional rules."""

from .models.api.catalog_api_teach_course_substitutes import (
    CATALOG_COURSE_SUBSTITUTES,
    CourseSubstitutesResponse,
)
from .sqlite_store import SQLiteModelStore
from .utils.auth import RequestSession

COURSE_SUBSTITUTES_URL = (
    "https://catalog.ustc.edu.cn/api/teach/course-substitute-pool/list"
)


async def refresh_course_substitutions(
    session: RequestSession, store: SQLiteModelStore
) -> None:
    payload = await session.get_json(
        COURSE_SUBSTITUTES_URL, timeout=60_000, transient_retries=0
    )
    response = CourseSubstitutesResponse.model_validate(payload)
    if len({group.id for group in response.root}) != len(response.root):
        raise ValueError("The official substitution pool repeated a rule ID")

    # Distinct rule IDs can describe the same course sets. Keep every source
    # record and both sides, including reverse rules, with their original order.
    with store.conn:
        store.register_response_model(
            table_name=CATALOG_COURSE_SUBSTITUTES,
            response_model=CourseSubstitutesResponse,
        )
        old_fetches = [
            fetch_id
            for (fetch_id,) in store.conn.execute(
                "SELECT id FROM upstream_fetches WHERE source = ?",
                (CATALOG_COURSE_SUBSTITUTES,),
            )
        ]
        store.delete_fetches(old_fetches)
        fetch_id = store.record_fetch(
            source=CATALOG_COURSE_SUBSTITUTES, method="GET", url=COURSE_SUBSTITUTES_URL
        )
        count = store.store_response(
            table_name=CATALOG_COURSE_SUBSTITUTES,
            response=response,
            fetch_id=fetch_id,
        )
        store.put_metadata({f"{CATALOG_COURSE_SUBSTITUTES}_count": count})
