from pydantic import Field, RootModel

from .base import UpstreamBaseModel, UpstreamSnapshotModel

CATALOG_COURSE_SUBSTITUTES = "catalog_teach_course_substitute_pool_list"


class CourseSubstituteMember(UpstreamBaseModel):
    id: int = Field(gt=0)
    cn: str
    en: str
    code: str = Field(min_length=1)
    period: int = Field(ge=0)
    credits: int | float = Field(ge=0, allow_inf_nan=False)


class CourseSubstituteGroup(UpstreamSnapshotModel):
    id: int = Field(gt=0)
    original_courses: list[CourseSubstituteMember] = Field(
        alias="originalCourses", min_length=1
    )
    substitute_courses: list[CourseSubstituteMember] = Field(
        alias="substituteCourses", min_length=1
    )


class CourseSubstitutesResponse(RootModel[list[CourseSubstituteGroup]]):
    root: list[CourseSubstituteGroup] = Field(min_length=1)
