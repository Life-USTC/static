from __future__ import annotations

from pydantic import RootModel

from .base import UpstreamBaseModel as BaseModel


class DepartmentTreeItem(BaseModel):
    id: int | None
    code: str | None
    nameZh: str | None = None
    name: str | None = None
    nameEn: str | None = None
    college: bool | None = None
    isCollege: bool | None = None
    children: list[DepartmentTreeItem] | None = None


class DepartmentCollegeTreeResponse(RootModel[list[DepartmentTreeItem] | None]):
    root: list[DepartmentTreeItem] | None
