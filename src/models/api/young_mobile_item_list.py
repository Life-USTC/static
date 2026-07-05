from __future__ import annotations

from typing import Any

from .base import UpstreamBaseModel as BaseModel


class YoungMobileItemListResult(BaseModel):
    records: list[dict[str, Any]] | None
    total: int | None
    size: int | None
    current: int | None
    orders: list[dict[str, Any]] | None
    searchCount: bool | None
    pages: int | None


class YoungMobileItemListResponse(BaseModel):
    success: bool | None
    message: str | None
    code: int | None
    result: YoungMobileItemListResult | None
    timestamp: int | None = None
