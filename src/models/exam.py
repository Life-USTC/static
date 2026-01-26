
from pydantic import BaseModel


class Exam(BaseModel):
    startDate: int
    endDate: int
    name: str
    location: str
    examType: str
    startHHMM: int
    endHHMM: int
    examMode: str | None
    additionalInfo: dict[str, str]
