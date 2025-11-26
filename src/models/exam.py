from pydantic import BaseModel
from typing import Optional


class Exam(BaseModel):
    startDate: int
    endDate: int
    name: str
    location: str
    examType: str
    startHHMM: int
    endHHMM: int
    examMode: Optional[str]
    additionalInfo: dict[str, str]
