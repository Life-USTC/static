from pydantic import Field, RootModel

from .base import UpstreamBaseModel, UpstreamSnapshotModel


class CourseLocalizedText(UpstreamBaseModel):
    cn: str | None
    en: str | None


class CourseSyllabusPeriod(UpstreamBaseModel):
    experiment: str | None
    period: int | float | None
    name_zh: str | None = Field(alias="nameZh")


class CourseSyllabus(UpstreamBaseModel):
    template: int | None
    # The catalogue encodes the syllabus document as an inner JSON string.
    documents: str | None
    periods: list[CourseSyllabusPeriod] | None


class CourseTextbook(UpstreamBaseModel):
    name_zh: str | None = Field(alias="nameZh")
    name_en: str | None = Field(alias="nameEn")
    publish: int | None
    author: str | None
    isbn: str | None
    publishing_house: str | None = Field(alias="publishingHouse")
    edition: str | None
    dates: str | None
    price: int | float | str | None
    type: str | None
    grade: str | int | None
    content: str | None
    remark: str | None


class TeachCourseInfo(UpstreamSnapshotModel):
    id: int = Field(gt=0)
    code: str = Field(min_length=1)
    course_type: str | None = Field(alias="courseType")
    dept: str | None
    credit: int | float | None
    hour: int | float | None
    grading: str | None
    exam_type: str | None = Field(alias="examType")
    lang: str | None
    textbook: str | None
    ref: str | None
    last_term: str | None = Field(alias="lastTerm")
    valid: bool
    discipline: str | None
    course_category: str | None = Field(alias="courseCategory")
    course_classify: str | None = Field(alias="courseClassify")
    sem: str | None
    preq: str | None
    syllabus: CourseSyllabus | None
    textbooks: list[CourseTextbook] | None
    name: CourseLocalizedText | None
    desc: CourseLocalizedText | None


class TeachCourseInfosResponse(RootModel[list[TeachCourseInfo]]):
    pass
