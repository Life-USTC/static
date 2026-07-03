from pydantic import BaseModel, Field

from .exam import Exam
from .lecture import Lecture


class TeacherAssignment(BaseModel):
    name: str
    nameEn: str | None = None
    code: str | None = None
    teacherId: int | None = None
    personId: int | None = None
    department: str | None = None
    departmentCode: str | None = None
    role: str | None = None
    indexNo: int | None = None
    age: int | None = None
    title: str | None = None
    period: float | None = None
    teacherLessonType: str | None = None
    teacherLessonTypeCode: str | None = None
    teacherLessonTypeRole: str | None = None
    weekIndices: list[int] = Field(default_factory=list)
    weekIndicesMsg: str | None = None


class Course(BaseModel):
    id: int
    name: str
    courseCode: str
    lessonCode: str
    teacherName: str
    teacherAssignments: list[TeacherAssignment] = Field(default_factory=list)
    lectures: list[Lecture]
    exams: list[Exam]
    dateTimePlacePersonText: str | None
    courseType: str | None
    courseGradation: str
    courseCategory: str
    educationType: str
    classType: str
    openDepartment: str
    description: str
    credit: float
    additionalInfo: dict[str, str]
