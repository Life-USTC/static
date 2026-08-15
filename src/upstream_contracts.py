from pydantic import BaseModel, RootModel

from .models.api.catalog_api_teach_department_college_tree import (
    DepartmentCollegeTreeResponse,
)
from .models.api.catalog_api_teach_exam_list import TeachExamListResponse
from .models.api.catalog_api_teach_lesson_list_for_teach import (
    TeachLessonListResponse,
)
from .models.api.catalog_api_teach_semester_list import TeachSemesterListResponse
from .models.api.jw_ws_schedule_table_datum import JwWsScheduleTableDatumResponse
from .models.api.young_mobile_item_list import YoungMobileItemListResponse

type UpstreamResponseModel = type[BaseModel] | type[RootModel]

CATALOG_SEMESTERS = "catalog_teach_semester_list"
CATALOG_DEPARTMENTS = "catalog_teach_department_college_tree"
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
    "young_mobile_item_enrolment_list": YoungMobileItemListResponse,
    "young_mobile_item_end_list": YoungMobileItemListResponse,
}
