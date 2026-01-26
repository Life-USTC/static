import asyncio

from ..models import Course, Exam, Semester
from ..models.api.catalog_api_teach_exam_list import TeachExamListResponse
from ..models.api.catalog_api_teach_lesson_list_for_teach import (
    TeachLessonListResponse,
)
from ..models.api.catalog_api_teach_semester_list import (
    TeachSemesterListResponse,
)
from .auth import RequestSession
from .tools import compose_start_end, join_nonempty, raw_date_to_unix_timestamp


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
                courses=[],
                name=item.nameZh or "",
                startDate=start_date,
                endDate=end_date,
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
    parsed = TeachLessonListResponse.model_validate(payload)

    result = []
    for item in parsed.root or []:
        if not item:
            continue
        teacher_names = [
            ta.cn for ta in (item.teacherAssignmentList or []) if ta and ta.cn
        ]
        teachers = join_nonempty(teacher_names)
        course = item.course
        result.append(
            Course(
                id=item.id or 0,
                name=course.cn if course and course.cn else "",
                courseCode=course.code if course and course.code else "",
                lessonCode=item.code or "",
                teacherName=teachers,
                lectures=[],
                exams=[],
                dateTimePlacePersonText=item.dateTimePlacePersonText.cn
                if item.dateTimePlacePersonText
                else None,
                courseType=item.courseType.cn if item.courseType else None,
                courseGradation=item.courseGradation.cn
                if item.courseGradation and item.courseGradation.cn
                else "",
                courseCategory=item.courseCategory.cn
                if item.courseCategory and item.courseCategory.cn
                else "",
                educationType=item.education.cn
                if item.education and item.education.cn
                else "",
                classType=item.classType.cn
                if item.classType and item.classType.cn
                else "",
                openDepartment=item.openDepartment.cn
                if item.openDepartment and item.openDepartment.cn
                else "",
                description="",
                credit=item.credits or 0,
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
    parsed = TeachExamListResponse.model_validate(payload)

    result: dict[int, list[Exam]] = {}
    for exam_item in parsed.root or []:
        if not exam_item:
            continue
        if not exam_item.examDate:
            continue
        if exam_item.startTime is None or exam_item.endTime is None:
            continue
        if not exam_item.lesson or not exam_item.lesson.course:
            continue
        if exam_item.lesson.id is None:
            continue
        room_list = [er.room for er in (exam_item.examRooms or []) if er and er.room]
        location = ", ".join(room_list)

        startHHMM = int(exam_item.startTime)
        endHHMM = int(exam_item.endTime)
        startDate, endDate = compose_start_end(exam_item.examDate, startHHMM, endHHMM)

        examType = "Unknown"
        if exam_item.examType == 1:
            examType = "期中考试"
        elif exam_item.examType == 2:
            examType = "期末考试"

        name = exam_item.lesson.course.cn or ""
        lesson_id = int(exam_item.lesson.id)

        exam = Exam(
            startDate=startDate,
            endDate=endDate,
            name=name,
            location=location,
            examType=examType,
            startHHMM=startHHMM,
            endHHMM=endHHMM,
            examMode=exam_item.examMode or "",
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
