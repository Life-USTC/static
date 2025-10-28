from pydantic import BaseModel


class Exam(BaseModel):
    startDate: int  # unix timestamp
    endDate: int  # unix timestamp
    name: str
    location: str
    examType: str  # 期中/期末
    startHHMM: int
    endHHMM: int
    examMode: str  # 开卷/闭卷
    additionalInfo: dict[str, str]
