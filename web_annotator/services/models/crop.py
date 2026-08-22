"""裁剪页模型.

Callers:
    - ``services.models``: 包导出.
    - ``routes.crop``: 裁剪路由.
    - ``services.dataset.crop``: 裁剪匹配.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import ImageThumbItem


class CropMatchConfig(BaseModel):
    """裁剪边长网格配置."""

    side_min: int
    side_max: int
    side_step: int
    zooms: list[float]
    max_output_pixels: int


class CropImageItem(ImageThumbItem):
    """裁剪页图片列表项."""

    done: bool


class CropPageConfig(BaseModel):
    """裁剪页配置."""

    parts: list[str]


class CropSavedItem(BaseModel):
    """单次裁剪保存项."""

    file: str
    size: list[int]
    name: str
    crop: list[int] | None = None


class CropSaveResponse(BaseModel):
    """裁剪保存响应."""

    saved: list[CropSavedItem]
    count: int

