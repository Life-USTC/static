from dataclasses import dataclass


@dataclass(frozen=True)
class Semester:
    id: str
    name: str
    startDate: int
    endDate: int
