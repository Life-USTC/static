from pydantic import Field, RootModel

from .base import UpstreamSnapshotModel


class TeachCourseSearchItem(UpstreamSnapshotModel):
    number: str = Field(min_length=1)
    name: str | None
    ename: str | None
    valid: bool
    last_term: str | None = Field(alias="lastTerm")
    dept: str | None


class TeachCourseSearchResponse(RootModel[list[TeachCourseSearchItem]]):
    pass
