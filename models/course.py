from pydantic import BaseModel

from .lecture import Lecture
from .exam import Exam


class Course(BaseModel):
    id: str
    name: str
    courseCode: str
    lessonCode: str
    teacherName: str
    lectures: list[Lecture]
    exams: list[Exam]
    dateTimePlacePersonText: str
    courseType: str
    courseGradation: str
    courseCategory: str
    educationType: str
    classType: str
    openDepartment: str
    description: str
    credit: float
    additionalInfo: dict[str, str]
