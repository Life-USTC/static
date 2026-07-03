from pydantic import BaseModel


class Department(BaseModel):
    code: str
    name: str
    nameEn: str | None = None
    parentCode: str | None = None
    isCollege: bool | None = None
