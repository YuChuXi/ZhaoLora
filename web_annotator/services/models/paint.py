"""蒙版绘制模型.

Callers:
    - ``services.models``: 包导出.
    - ``routes.alpha_paint``: 蒙版绘制路由.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import ImageThumbItem


class PaintSaveResponse(BaseModel):
    """蒙版保存回执."""

    saved: str
    size: list[int]


class DefaultAlphaResponse(BaseModel):
    """默认透明度配置."""

    default_alpha: int


class PaintImageItem(ImageThumbItem):
    """蒙版页图片列表项.

    ``width`` / ``height`` 为原图尺寸(视觉预览已压缩, 蒙版须按原尺寸绘制).
    """

    has_mask: bool

