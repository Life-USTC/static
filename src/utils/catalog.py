import asyncio

from ..models import Course, Exam, Semester
from ..models.api.catalog_api_teach_exam_list import Model as ExamListModel
from ..models.api.catalog_api_teach_lesson_list_for_teach import (
    Model as CourseListModel,
)
from ..models.api.catalog_api_teach_semester_list import (
    Model as SemesterListModel,
)
from .auth import RequestSession
from .tools import compose_start_end, join_nonempty, raw_date_to_unix_timestamp


async def fetch_semesters_json(session: RequestSession) -> list[dict]:
    url = "https://catalog.ustc.edu.cn/api/teach/semester/list"
    return await session.get_json(url=url)


def parse_semesters(payload: list[dict]) -> list[Semester]:
    parsed = SemesterListModel(payload)

    result = []
    for item in parsed.root:
        result.append(
            Semester(
                id=str(item.id),
                courses=[],
                name=item.nameZh,
                startDate=raw_date_to_unix_timestamp(item.start),
                endDate=raw_date_to_unix_timestamp(item.end),
            )
        )
    return result


async def get_semesters(session: RequestSession) -> list[Semester]:
    payload = await fetch_semesters_json(session=session)
    return parse_semesters(payload)


async def fetch_courses_json(session: RequestSession, semester_id: str) -> list[dict]:
    await asyncio.sleep(10)
    url = "https://catalog.ustc.edu.cn/api/teach/lesson/list-for-teach/" + semester_id
    return await session.get_json(url=url)


def parse_courses(payload: list[dict]) -> list[Course]:
    parsed = CourseListModel(payload)

    result = []
    for item in parsed.root:
        teacher_names = [ta.cn for ta in item.teacherAssignmentList]
        teachers = join_nonempty(teacher_names)
        result.append(
            Course(
                id=item.id,
                name=item.course.cn,
                courseCode=item.course.code,
                lessonCode=item.code,
                teacherName=teachers,
                lectures=[],
                exams=[],
                dateTimePlacePersonText=item.dateTimePlacePersonText.cn,
                courseType=item.courseType.cn if item.courseType else None,
                courseGradation=item.courseGradation.cn,
                courseCategory=item.courseCategory.cn,
                educationType=item.education.cn,
                classType=item.classType.cn,
                openDepartment=item.openDepartment.cn,
                description="",
                credit=item.credits,
                additionalInfo={},
            )
        )
    return result


async def get_courses(session: RequestSession, semester_id: str) -> list[Course]:
    payload = await fetch_courses_json(session=session, semester_id=semester_id)
    return parse_courses(payload)


async def fetch_exams_json(session: RequestSession, semester_id: str) -> list[dict]:
    await asyncio.sleep(10)
    url = f"https://catalog.ustc.edu.cn/api/teach/exam/list/{semester_id}"
    return await session.get_json(url=url)


def parse_exams(payload: list[dict]) -> dict[int, list[Exam]]:
    parsed = ExamListModel(payload)

    result: dict[int, list[Exam]] = {}
    for exam_item in parsed.root:
        room_list = [er.room for er in exam_item.examRooms]
        location = ", ".join(room_list)

        startHHMM = exam_item.startTime
        endHHMM = exam_item.endTime
        startDate, endDate = compose_start_end(exam_item.examDate, startHHMM, endHHMM)

        examType = "Unknown"
        if exam_item.examType == 1:
            examType = "期中考试"
        elif exam_item.examType == 2:
            examType = "期末考试"

        name = exam_item.lesson.course.cn
        lesson_id = exam_item.lesson.id

        exam = Exam(
            startDate=startDate,
            endDate=endDate,
            name=name,
            location=location,
            examType=examType,
            startHHMM=startHHMM,
            endHHMM=endHHMM,
            examMode=exam_item.examMode,
            additionalInfo={},
        )
        if lesson_id in result:
            result[lesson_id].append(exam)
        else:
            result[lesson_id] = [exam]

    return result


async def get_exams(session: RequestSession, semester_id: str) -> dict[int, list[Exam]]:
    payload = await fetch_exams_json(session=session, semester_id=semester_id)
    return parse_exams(payload)
