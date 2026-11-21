"""Refresh the official course substitution pool without merging directional rules."""

from src.http import RequestSession
from src.models.api.catalog_api_teach_course_substitutes import (
    CATALOG_COURSE_SUBSTITUTES,
    CourseSubstitutesResponse,
)
from src.sqlite_store import SQLiteModelStore

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
        store.delete_source_fetches(CATALOG_COURSE_SUBSTITUTES)
        count = store.store_fetched_response(
            CATALOG_COURSE_SUBSTITUTES, COURSE_SUBSTITUTES_URL, response
        )
        store.put_metadata({f"{CATALOG_COURSE_SUBSTITUTES}_count": count})
