"""裁剪页模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/crop.py:`` : 裁剪路由.
    - ``services/dataset/crop.py:`` : 裁剪匹配.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CropMatchConfig(BaseModel):
    """裁剪边长网格配置."""

    side_min: int
    side_max: int
    side_step: int
    zooms: list[float]
    max_output_pixels: int


class CropPartRecord(BaseModel):
    """单个部位输出的裁剪记录.

    ``crop`` 为源图像素坐标 ``[x1, y1, x2, y2]``; 整图输出或未记录坐标时为 None.
    ``size`` 为输出像素尺寸 ``[宽, 高]``; 未记录时为 None.
    """

    crop: list[int] | None = None
    size: list[int] | None = None


class CropImageState(BaseModel):
    """单张源图的部位输出状态(部位目录名 -> 输出记录).

    同时用作 ``.crops/{stem}.json`` 的持久化结构与 ``/crop/state`` 端点响应.
    """

    stem: str
    parts: dict[str, CropPartRecord] = Field(default_factory=dict)


class CropPageConfig(BaseModel):
    """裁剪页配置."""

    parts: list[str] = Field(default_factory=list, description="已存在的部位目录名列表")


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

