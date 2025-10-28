from pydantic import BaseModel


class Lecture(BaseModel):
    startDate: int  # unix timestamp
    endDate: int  # unix timestamp
    name: str
    location: str
    teacherName: str
    periods: float
    startIndex: int
    endIndex: int
    startHHMM: int
    endHHMM: int
    additionalInfo: dict[str, str]
