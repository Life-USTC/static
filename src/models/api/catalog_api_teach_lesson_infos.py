from pydantic import Field, RootModel

from .catalog_api_teach_course_infos import TeachCourseInfo


class TeachLessonInfo(TeachCourseInfo):
    course_id: int = Field(alias="courseId", gt=0)
    lesson_language: str | None = Field(default=None, alias="lessonLang")


class TeachLessonInfosResponse(RootModel[list[TeachLessonInfo]]):
    pass
