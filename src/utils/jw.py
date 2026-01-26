import asyncio
import logging

from bs4 import BeautifulSoup

from ..models import Course, Lecture
from ..models.api.jw_for_std_lesson_search_semester import (
    JwForStdLessonSearchSemesterResponse,
)
from ..models.api.jw_ws_schedule_table_datum import (
    JwWsScheduleTableDatumResponse,
)
from .auth import RequestSession
from .tools import cache_dir_from_url, compose_start_end, join_nonempty, save_json

_jw_user_id_cache: dict[int, str] = {}

indexStartTimes: dict[int, int] = {
    1: 7 * 60 + 50,
    2: 8 * 60 + 40,
    3: 9 * 60 + 45,
    4: 10 * 60 + 35,
    5: 11 * 60 + 25,
    6: 14 * 60 + 0,
    7: 14 * 60 + 50,
    8: 15 * 60 + 35,
    9: 16 * 60 + 45,
    10: 17 * 60 + 35,
    11: 19 * 60 + 30,
    12: 20 * 60 + 20,
    13: 21 * 60 + 10,
}

endIndexTimes: dict[int, int] = {
    1: 8 * 60 + 35,
    2: 9 * 60 + 25,
    3: 10 * 60 + 30,
    4: 11 * 60 + 20,
    5: 12 * 60 + 10,
    6: 14 * 60 + 45,
    7: 15 * 60 + 35,
    8: 16 * 60 + 40,
    9: 17 * 60 + 30,
    10: 18 * 60 + 20,
    11: 20 * 60 + 15,
    12: 21 * 60 + 5,
    13: 21 * 60 + 55,
}


def findNearestIndex(time: int, times: dict[int, int]) -> int:
    map = {}
    for index, t in times.items():
        map[abs(time - t)] = index
    return map[min(map.keys())]


def cleanLectures(lectures: list[Lecture]) -> list[Lecture]:
    result = []

    for lecture in lectures:
        for r in result:
            if lecture.startDate >= r.startDate and lecture.endDate <= r.endDate:
                if lecture.teacherName not in r.teacherName:
                    r.teacherName += "," + lecture.teacherName
                if lecture.location not in r.location:
                    r.location += "," + lecture.location
                break
            elif lecture.endDate == r.startDate:
                r.startDate = lecture.startDate
                r.startIndex = lecture.startIndex
                break
            elif lecture.startDate == r.endDate:
                r.endDate = lecture.endDate
                r.endIndex = lecture.endIndex
                break
        else:
            result.append(lecture)
    return result


async def _get_jw_user_id(session: RequestSession) -> str:
    if _jw_user_id_cache:
        return list(_jw_user_id_cache.values())[0]

    url = "https://jw.ustc.edu.cn/for-std/course-select"
    r = await session.get(url=url)
    final_url = getattr(r, "url", "")
    user_id = str(final_url).split("/")[-1]
    if not user_id.isnumeric():
        raise ValueError(f"Failed to get jw user id from url: {final_url}")

    _jw_user_id_cache[id(session)] = user_id
    return user_id


async def _get_jw_semester_options(
    session: RequestSession, user_id: str
) -> list[tuple[str, str]]:
    index_url = f"https://jw.ustc.edu.cn/for-std/lesson-search/index/{user_id}"
    r = await session.get(url=index_url)
    html = await r.text()
    soup = BeautifulSoup(html, "html.parser")
    options = soup.select("select#semester option")
    if not options:
        raise ValueError("Failed to find semester options from jw")

    result = []
    for option in options:
        raw_value = option.get("value", "")
        if isinstance(raw_value, list):
            raw_value = raw_value[0] if raw_value else ""
        if raw_value is None:
            raw_value = ""
        semester_id = str(raw_value).strip()
        semester_name = option.text.strip()
        if semester_id and semester_name:
            result.append((semester_id, semester_name))
    return result


async def fetch_jw_courses_json(session: RequestSession, semester_id: str) -> dict:
    await asyncio.sleep(10)
    user_id = await _get_jw_user_id(session=session)
    query = (
        "courseCodeLike=&codeLike=&educationAssoc=&courseNameZhLike=&teacherNameLike=&"
        "schedulePlace=&classCodeLike=&courseTypeAssoc=&classTypeAssoc=&campusAssoc=&"
        "teachLangAssoc=&roomTypeAssoc=&examModeAssoc=&requiredPeriodInfo.totalGte=&"
        "requiredPeriodInfo.totalLte=&requiredPeriodInfo.weeksGte=&requiredPeriodInfo.weeksLte=&"
        "requiredPeriodInfo.periodsPerWeekGte=&requiredPeriodInfo.periodsPerWeekLte=&"
        "limitCountGte=&limitCountLte=&majorAssoc=&majorDirectionAssoc=&"
        "queryPage__=1%2C100000"
    )
    url = (
        "https://jw.ustc.edu.cn/for-std/lesson-search/semester/"
        + semester_id
        + "/search/"
        + user_id
        + "?"
        + query
    )
    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "x-requested-with": "XMLHttpRequest",
        "referer": f"https://jw.ustc.edu.cn/for-std/lesson-search/index/{user_id}",
    }

    return await session.get_json(url=url, headers=headers)


def parse_jw_courses(payload: dict) -> list[Course]:
    parsed = JwForStdLessonSearchSemesterResponse.model_validate(payload)

    result = []
    for lesson_item in parsed.data or []:
        if not lesson_item:
            continue
        teacher_names = [
            ta.person.nameZh
            for ta in (lesson_item.teacherAssignmentList or [])
            if ta and ta.person and ta.person.nameZh
        ]
        teachers = join_nonempty(teacher_names)
        course_type_name = (
            lesson_item.courseType.nameZh if lesson_item.courseType else ""
        )
        course = lesson_item.course
        result.append(
            Course(
                id=lesson_item.id or 0,
                name=course.nameZh if course and course.nameZh else "",
                courseCode=course.code if course and course.code else "",
                lessonCode=lesson_item.code or "",
                teacherName=teachers,
                lectures=[],
                exams=[],
                dateTimePlacePersonText=lesson_item.scheduleText.dateTimePlacePersonText.textZh
                if lesson_item.scheduleText
                and lesson_item.scheduleText.dateTimePlacePersonText
                and lesson_item.scheduleText.dateTimePlacePersonText.textZh
                else None,
                courseType=course_type_name or None,
                courseGradation=lesson_item.courseGradation.nameZh
                if lesson_item.courseGradation and lesson_item.courseGradation.nameZh
                else "",
                courseCategory=lesson_item.courseCategory.nameZh
                if lesson_item.courseCategory and lesson_item.courseCategory.nameZh
                else "",
                educationType=lesson_item.education.nameZh
                if lesson_item.education and lesson_item.education.nameZh
                else "",
                classType=lesson_item.classType.nameZh
                if lesson_item.classType and lesson_item.classType.nameZh
                else "",
                openDepartment=lesson_item.openDepartment.simpleNameZh
                if lesson_item.openDepartment
                and lesson_item.openDepartment.simpleNameZh
                else "",
                description=lesson_item.introduction or "",
                credit=lesson_item.credits or 0,
                additionalInfo={},
            )
        )
    return result


async def get_courses(session: RequestSession, semester_id: str) -> list[Course]:
    payload = await fetch_jw_courses_json(session=session, semester_id=semester_id)
    return parse_jw_courses(payload)


async def fetch_jw_schedule_table_json(
    session: RequestSession, course_list: list[Course]
) -> dict:
    url = "https://jw.ustc.edu.cn/ws/schedule-table/datum"
    course_id_list = [str(course.id) for course in course_list]
    return await session.post_json(url=url, data={"lessonIds": course_id_list})


def parse_jw_schedule_table(
    course_list: list[Course], payload: dict, *, cache_url: str | None = None
) -> list[Course]:
    logger = logging.getLogger(__name__)
    parsed = JwWsScheduleTableDatumResponse.model_validate(payload)
    if not parsed.result:
        return course_list

    if cache_url:
        for course in course_list:
            save_course_json: dict = {"result": {}}
            save_course_json["result"]["lessonList"] = [
                item.model_dump()
                for item in (parsed.result.lessonList or [])
                if item.id == course.id
            ]
            save_course_json["result"]["scheduleList"] = [
                item.model_dump()
                for item in (parsed.result.scheduleList or [])
                if item.lessonId == course.id
            ]
            save_course_json["result"]["scheduleGroupList"] = [
                item.model_dump()
                for item in (parsed.result.scheduleGroupList or [])
                if item.lessonId == course.id
            ]

            save_json(
                save_course_json,
                cache_dir_from_url(cache_url) / f"{course.id}.json",
            )

    for schedule_item in parsed.result.scheduleList or []:
        if not schedule_item:
            continue
        if schedule_item.lessonId is None:
            continue
        if schedule_item.startTime is None or schedule_item.endTime is None:
            continue
        if not schedule_item.date:
            continue
        course = next(
            (course for course in course_list if course.id == schedule_item.lessonId),
            None,
        )
        if not course:
            continue

        startHHMM = int(schedule_item.startTime)
        endHHMM = int(schedule_item.endTime)
        startDate, endDate = compose_start_end(schedule_item.date, startHHMM, endHHMM)

        location = (
            schedule_item.room.nameZh
            if schedule_item.room and schedule_item.room.nameZh
            else schedule_item.customPlace or ""
        )

        startIndex = findNearestIndex(
            int(startHHMM // 100) * 60 + int(startHHMM % 100), indexStartTimes
        )
        endIndex = findNearestIndex(
            int(endHHMM // 100) * 60 + int(endHHMM % 100), endIndexTimes
        )

        lecture = Lecture(
            startDate=startDate,
            endDate=endDate,
            name=course.name,
            location=location,
            teacherName=schedule_item.personName if schedule_item.personName else "",
            periods=schedule_item.periods if schedule_item.periods else 0,
            additionalInfo={},
            startIndex=startIndex,
            endIndex=endIndex,
            startHHMM=startHHMM,
            endHHMM=endHHMM,
        )

        course.lectures.append(lecture)

    for course in course_list:
        course.lectures = cleanLectures(course.lectures)
        logger.info(f"course {course.id} lectures count {len(course.lectures)}")

    return course_list


async def update_lectures(
    session: RequestSession, course_list: list[Course]
) -> list[Course]:
    url = "https://jw.ustc.edu.cn/ws/schedule-table/datum"
    payload = await fetch_jw_schedule_table_json(
        session=session, course_list=course_list
    )
    return parse_jw_schedule_table(course_list, payload, cache_url=url)
