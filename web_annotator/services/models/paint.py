"""蒙版绘制模型。

引用: ``services.models`` 包导出；各 routes / services。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PaintSaveResponse(BaseModel):
    """蒙版保存回执。

    引用:
        ``routes.alpha_paint``。
    """

    saved: str
    size: list[int]


class DefaultAlphaResponse(BaseModel):
    """默认透明度配置。

    引用:
        ``routes.alpha_paint``。
    """

    default_alpha: int


class PaintImageItem(BaseModel):
    """蒙版页图片列表项。

    引用:
        ``routes.alpha_paint.list_paint_images``。
    """

    path: str
    stem: str
    has_mask: bool

