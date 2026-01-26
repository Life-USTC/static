
from pydantic import BaseModel

from .exam import Exam
from .lecture import Lecture


class Course(BaseModel):
    id: int
    name: str
    courseCode: str
    lessonCode: str
    teacherName: str
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
