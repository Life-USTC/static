from typing import Optional
from pydantic import BaseModel

from .lecture import Lecture
from .exam import Exam


class Course(BaseModel):
    id: int
    name: str
    courseCode: str
    lessonCode: str
    teacherName: str
    lectures: list[Lecture]
    exams: list[Exam]
    dateTimePlacePersonText: Optional[str]
    courseType: Optional[str]
    courseGradation: str
    courseCategory: str
    educationType: str
    classType: str
    openDepartment: str
    description: str
    credit: float
    additionalInfo: dict[str, str]
