from __future__ import annotations

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

UPSTREAM_RESPONSE_MODELS: dict[str, UpstreamResponseModel] = {
    "catalog_teach_semester_list": TeachSemesterListResponse,
    "catalog_teach_department_college_tree": DepartmentCollegeTreeResponse,
    "catalog_teach_lesson_list_for_teach": TeachLessonListResponse,
    "catalog_teach_exam_list": TeachExamListResponse,
    "jw_ws_schedule_table_datum": JwWsScheduleTableDatumResponse,
    "young_mobile_item_enrolment_list": YoungMobileItemListResponse,
    "young_mobile_item_end_list": YoungMobileItemListResponse,
}
