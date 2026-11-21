import json

from src.http import RequestSession
from src.models.api.catalog_api_teach_semester_list import (
    TeachSemesterListResponse,
)
from src.models.semester import Semester
from src.utils.tools import raw_date_to_unix_timestamp


async def fetch_semesters_json(session: RequestSession) -> list[dict]:
    url = "https://catalog.ustc.edu.cn/api/teach/semester/list"
    return await session.get_json(url=url)


def parse_semesters(payload: list[dict]) -> list[Semester]:
    parsed = TeachSemesterListResponse.model_validate(payload)

    result = []
    for item in parsed.root or []:
        if not item:
            continue
        start_date = raw_date_to_unix_timestamp(item.start) if item.start else 0
        end_date = raw_date_to_unix_timestamp(item.end) if item.end else 0
        result.append(
            Semester(
                id=str(item.id) if item.id is not None else "",
                name=item.nameZh or "",
                startDate=start_date,
                endDate=end_date,
            )
        )
    return result


async def fetch_departments_json(session: RequestSession) -> list[dict]:
    url = "https://catalog.ustc.edu.cn/api/teach/department/college-tree"
    return await session.get_json(url=url)


async def fetch_courses_json(session: RequestSession, semester_id: str) -> list[dict]:
    url = "https://catalog.ustc.edu.cn/api/teach/lesson/list-for-teach/" + semester_id
    return await session.get_json(url=url, timeout=60_000, transient_retries=0)


async def fetch_exams_json(
    session: RequestSession, semester_id: str, **request_kwargs
) -> list[dict]:
    url = f"https://catalog.ustc.edu.cn/api/teach/exam/list/{semester_id}"
    return await session.get_json(url=url, **request_kwargs)


def catalog_json(value: object, *, sort_keys=False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=sort_keys,
    )
