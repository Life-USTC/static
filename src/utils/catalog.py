import asyncio

from ..models import Course, Exam, Semester
from .auth import RequestSession
from .tools import (
    compose_start_end,
    join_nonempty,
    raw_date_to_unix_timestamp,
)


async def get_semesters(session: RequestSession) -> list[Semester]:
    url = "https://catalog.ustc.edu.cn/api/teach/semester/list"

    json = await session.get_json(url=url)

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


async def get_courses(session: RequestSession, semester_id: str) -> list[Course]:
    await asyncio.sleep(10)
    url = "https://catalog.ustc.edu.cn/api/teach/lesson/list-for-teach/" + semester_id

    json = await session.get_json(url=url)

    result = []
    for course_json in json:
        teacher_name_list = [
            teacher["cn"] for teacher in course_json["teacherAssignmentList"]
        ]
        teachers = join_nonempty(teacher_name_list)
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


async def get_exams(session: RequestSession, semester_id: str) -> dict[int, list[Exam]]:
    await asyncio.sleep(10)
    url = f"https://catalog.ustc.edu.cn/api/teach/exam/list/{semester_id}"

    json = await session.get_json(url=url)

    result = {}
    for exam_json in json:
        room_list = [room["room"] for room in exam_json["examRooms"]]
        location = ", ".join(room_list)

        startHHMM = int(exam_json["startTime"])
        endHHMM = int(exam_json["endTime"])
        startDate, endDate = compose_start_end(
            exam_json["examDate"], startHHMM, endHHMM
        )

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
