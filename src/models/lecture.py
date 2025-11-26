from pydantic import BaseModel


class Lecture(BaseModel):
    startDate: int
    endDate: int
    name: str
    location: str
    teacherName: str
    periods: float
    startIndex: int
    endIndex: int
    startHHMM: int
    endHHMM: int
    additionalInfo: dict[str, str]
