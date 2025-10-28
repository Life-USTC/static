import asyncio
import os
from json import dump
from patchright.async_api import Page

from models import Semester, Course, Exam
from utils.tools import raw_date_to_unix_timestamp


async def get_semesters(page: Page) -> list[Semester]:
    url = "https://catalog.ustc.edu.cn/api/teach/semester/list"

    response = await page.request.get(
        url=url,
        timeout=10 * 60 * 1000,
        fail_on_status_code=True,
        max_retries=100,
    )
    json = await response.json()
    # dump result json to $file/../build/cache/catalog/api/teach/semester/list.json
    os.makedirs("build/cache/catalog/api/teach/semester", exist_ok=True)
    dump(
        json,
        open("build/cache/catalog/api/teach/semester/list.json", "w"),
        ensure_ascii=False,
        indent=4,
    )

    # convert json to Semester

    result = []
    for semester_json in json:
        result.append(
            Semester(
                id=str(semester_json["id"]),
                courses=[],
                name=semester_json["nameZh"],
                startDate=raw_date_to_unix_timestamp(semester_json["start"]),
                endDate=raw_date_to_unix_timestamp(semester_json["end"]),
            )
        )
    return result


async def get_courses(page: Page, semester_id: str) -> list[Course]:
    await asyncio.sleep(10)
    url = "https://catalog.ustc.edu.cn/api/teach/lesson/list-for-teach/" + semester_id

    response = await page.request.get(
        url=url,
        timeout=10 * 60 * 1000,
        fail_on_status_code=True,
        max_retries=100,
    )
    json = await response.json()
    # dump result json to $file/../build/cache/catalog/api/teach/lesson/list-for-teach/$(semester_id).json
    os.makedirs("build/cache/catalog/api/teach/lesson/list-for-teach", exist_ok=True)
    dump(
        json,
        open(
            f"build/cache/catalog/api/teach/lesson/list-for-teach/{semester_id}.json",
            "w",
        ),
        ensure_ascii=False,
        indent=4,
    )

    # convert json to Course

    result = []
    for course_json in json:
        teacher_name_list = [
            teacher["cn"] for teacher in course_json["teacherAssignmentList"]
        ]
        # strip None:
        teacher_name_list = [
            teacher_name for teacher_name in teacher_name_list if teacher_name
        ]
        teachers = ", ".join(teacher_name_list)
        result.append(
            Course(
                id=course_json["id"],
                name=course_json["course"]["cn"],
                courseCode=course_json["course"]["code"],
                lessonCode=course_json["code"],
                teacherName=teachers,
                lectures=[],
                exams=[],
                dateTimePlacePersonText=course_json["dateTimePlacePersonText"]["cn"],
                courseType=course_json["courseType"]["cn"],
                courseGradation=course_json["courseGradation"]["cn"],
                courseCategory=course_json["courseCategory"]["cn"],
                educationType=course_json["education"]["cn"],
                classType=course_json["classType"]["cn"],
                openDepartment=course_json["openDepartment"]["cn"],
                description="",
                credit=course_json["credits"],
                additionalInfo={},
            )
        )
    return result


async def get_exams(
    page: Page,
    semester_id: str,
) -> dict[int, list[Exam]]:
    await asyncio.sleep(10)
    url = f"https://catalog.ustc.edu.cn/api/teach/exam/list/{semester_id}"

    response = await page.request.get(
        url=url,
        timeout=10 * 60 * 1000,
        fail_on_status_code=True,
        max_retries=100,
    )
    json = await response.json()
    # dump result json to $file/../build/cache/catalog/api/teach/exam/list/$(semester_id).json
    os.makedirs("build/cache/catalog/api/teach/exam/list", exist_ok=True)
    dump(
        json,
        open(f"build/cache/catalog/api/teach/exam/list/{semester_id}.json", "w"),
        ensure_ascii=False,
        indent=4,
    )

    result = {}
    for exam_json in json:
        room_list = [room["room"] for room in exam_json["examRooms"]]
        location = ", ".join(room_list)

        date = raw_date_to_unix_timestamp(exam_json["examDate"])
        startHHMM = int(exam_json["startTime"])
        endHHMM = int(exam_json["endTime"])

        startDate = date + int(startHHMM // 100) * 3600 + int(startHHMM % 100) * 60
        endDate = date + int(endHHMM // 100) * 3600 + int(endHHMM % 100) * 60

        examType = "Unknown"
        if exam_json["examType"] == 1:
            examType = "期中考试"
        elif exam_json["examType"] == 2:
            examType = "期末考试"
        examMode = exam_json["examMode"]

        name = exam_json["lesson"]["course"]["cn"]
        id: int = exam_json["lesson"]["id"]

        exam = Exam(
            startDate=startDate,
            endDate=endDate,
            name=name,
            location=location,
            examType=examType,
            startHHMM=startHHMM,
            endHHMM=endHHMM,
            examMode=examMode,
            additionalInfo={},
        )
        if id in result:
            result[id].append(exam)
        else:
            result[id] = [exam]

    return result
