import datetime
from pydantic import BaseModel

from .course import Course


class Semester(BaseModel):
    id: str
    courses: list[Course]
    name: str
    startDate: int
    endDate: int

    def __str__(self) -> str:
        date_fmt = lambda ts: datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        start_date = date_fmt(self.startDate)
        end_date = date_fmt(self.endDate)
        return f"Semester(id={self.id}, name={self.name}, {start_date} - {end_date})"
