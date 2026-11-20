from pydantic import BaseModel, RootModel

from .models.api.catalog_api_teach_course_infos import TeachCourseInfosResponse
from .models.api.catalog_api_teach_course_search import TeachCourseSearchResponse
from .models.api.catalog_api_teach_course_substitutes import (
    CATALOG_COURSE_SUBSTITUTES,
    CourseSubstitutesResponse,
)
from .models.api.catalog_api_teach_department_college_tree import (
    DepartmentCollegeTreeResponse,
)
from .models.api.catalog_api_teach_exam_list import TeachExamListResponse
from .models.api.catalog_api_teach_lesson_infos import TeachLessonInfosResponse
from .models.api.catalog_api_teach_lesson_list_for_teach import (
    TeachLessonListResponse,
)
from .models.api.catalog_api_teach_semester_list import TeachSemesterListResponse
from .models.api.jw_ws_schedule_table_datum import JwWsScheduleTableDatumResponse
from .models.api.young_mobile_item_list import YoungMobileItemListResponse

type UpstreamResponseModel = type[BaseModel] | type[RootModel]

CATALOG_SEMESTERS = "catalog_teach_semester_list"
CATALOG_DEPARTMENTS = "catalog_teach_department_college_tree"
CATALOG_COURSE_INFOS = "catalog_teach_course_infos"
CATALOG_COURSE_SEARCH = "catalog_teach_course_search"
CATALOG_LESSON_INFOS = "catalog_teach_lesson_infos"
CATALOG_LESSONS = "catalog_teach_lesson_list_for_teach"
CATALOG_EXAMS = "catalog_teach_exam_list"
JW_SCHEDULES = "jw_ws_schedule_table_datum"

CURRICULUM_UPSTREAM_RESPONSE_MODELS: dict[str, UpstreamResponseModel] = {
    CATALOG_SEMESTERS: TeachSemesterListResponse,
    CATALOG_DEPARTMENTS: DepartmentCollegeTreeResponse,
    CATALOG_LESSONS: TeachLessonListResponse,
    CATALOG_EXAMS: TeachExamListResponse,
    JW_SCHEDULES: JwWsScheduleTableDatumResponse,
}

UPSTREAM_RESPONSE_MODELS: dict[str, UpstreamResponseModel] = {
    **CURRICULUM_UPSTREAM_RESPONSE_MODELS,
    CATALOG_COURSE_SUBSTITUTES: CourseSubstitutesResponse,
    CATALOG_COURSE_INFOS: TeachCourseInfosResponse,
    CATALOG_COURSE_SEARCH: TeachCourseSearchResponse,
    CATALOG_LESSON_INFOS: TeachLessonInfosResponse,
    "young_mobile_item_enrolment_list": YoungMobileItemListResponse,
    "young_mobile_item_end_list": YoungMobileItemListResponse,
}
