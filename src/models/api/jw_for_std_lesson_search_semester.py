from .base import UpstreamBaseModel as BaseModel


class RoomType(BaseModel):
    nameZh: str | None
    nameEn: str | None
    id: int | None
    code: str | None
    enabled: bool | None
    bizTypeAssocs: list[int] | None
    bizTypeIds: list[int] | None
    transient: bool | None
    name: str | None


class FieldPage(BaseModel):
    currentPage: int | None
    rowsInPage: int | None
    rowsPerPage: int | None
    totalRows: int | None
    totalPages: int | None
