import logging

from models import Course, Lecture
from utils.tools import (
    cache_dir_from_url,
    compose_start_end,
    safe_symlink,
    save_json,
)
from utils.auth import RequestSession


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

# 0835 0925 0945 1030 1120 1445 1535 1555 1640 1730 2015 2105 2155
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
    """
    This handles the following situations:

    1. At the same time & place, sometimes jw.u.e.c would return two lectures, but with different teacher names, combine them as one.
    2. A Lecture taking place in non conventional time, for example 19:00 - 21:00 would be split into two lectures, combine them as one.
    """
    result = []

    for lecture in lectures:
        for r in result:
            if lecture.startDate >= r.startDate and lecture.endDate <= r.endDate:
                if not lecture.teacherName in r.teacherName:
                    r.teacherName += "," + lecture.teacherName
                if not lecture.location in r.location:
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


async def update_lectures(
    session: RequestSession, course_list: list[Course]
) -> list[Course]:
    logger = logging.getLogger(__name__)

    url = "https://jw.ustc.edu.cn/ws/schedule-table/datum"
    course_id_list = [str(course.id) for course in course_list]

    json = await session.post_json(url=url, data={"lessonIds": course_id_list})

    for course in course_list:
        course_json = {}
        course_json["result"] = {}
        course_json["result"]["lessonList"] = [
            item for item in json["result"]["lessonList"] if item["id"] == course.id
        ]
        course_json["result"]["scheduleList"] = [
            item
            for item in json["result"]["scheduleList"]
            if item["lessonId"] == course.id
        ]
        course_json["result"]["scheduleGroupList"] = [
            item
            for item in json["result"]["scheduleGroupList"]
            if item["lessonId"] == course.id
        ]

        save_json(
            course_json,
            cache_dir_from_url(url) / f"{course.id}.json",
        )

    json = json["result"]

    for schedule_json in json["scheduleList"]:
        course = [
            course for course in course_list if course.id == schedule_json["lessonId"]
        ][0]

        startHHMM = int(schedule_json["startTime"])
        endHHMM = int(schedule_json["endTime"])
        startDate, endDate = compose_start_end(
            schedule_json["date"], startHHMM, endHHMM
        )

        location = (
            schedule_json["room"]["nameZh"]
            if schedule_json["room"]
            else schedule_json["customPlace"]
        )
        if not location:
            location = ""

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
            teacherName=(
                schedule_json["personName"] if schedule_json["personName"] else ""
            ),
            periods=schedule_json["periods"] if schedule_json["periods"] else 0,
            additionalInfo={},
            startIndex=startIndex,
            endIndex=endIndex,
            startHHMM=startHHMM,
            endHHMM=endHHMM,
        )

        for course in course_list:
            if course.id == schedule_json["lessonId"]:
                course.lectures.append(lecture)
                break

    for course in course_list:
        course.lectures = cleanLectures(course.lectures)
        logger.info(f"course {course.id} lectures count {len(course.lectures)}")

    return course_list
