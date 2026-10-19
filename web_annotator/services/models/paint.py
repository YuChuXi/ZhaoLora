"""蒙版绘制模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/alpha_paint.py:`` : 蒙版绘制路由.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import ImageThumbItem


class PaintSaveResponse(BaseModel):
    """蒙版保存回执."""

    saved: str
    size: list[int]


class PaintImageItem(ImageThumbItem):
    """蒙版页图片列表项.

    ``width`` / ``height`` 为原图尺寸(视觉预览已压缩, 蒙版须按原尺寸绘制).
    """

    has_mask: bool


class TemplateAnchor(BaseModel):
    """模板锚点: 图案上的相对位置(0-1 分数坐标), 随模板持久化, 放置时随实例载入.

    Callers:
        - ``services/models/paint.py:TemplateItem``: 模板项的锚点列表.
    """

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class TemplateItem(BaseModel):
    """单个蒙板模板项.

    图案以 base64 PNG 编码无损传输(保留捕获时区域内各像素 alpha), 服务端按数据集持久化;
    ``anchors`` 为图案上的自定义锚点(≤2 个), 供放置后拖动对齐.

    Callers:
        - ``routes/alpha_paint.py:get_templates``
        - ``routes/alpha_paint.py:set_templates``
    """

    slot: int = Field(ge=0, le=9)
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    image_b64: str
    anchors: list[TemplateAnchor] = Field(default_factory=list, max_length=2)


class TemplatesPayload(BaseModel):
    """蒙板模板全量集(服务端按数据集持久化).

    PUT 以全量覆盖写入, GET 返回当前存储的全部模板(含各模板 base64 图案).

    Callers:
        - ``routes/alpha_paint.py:get_templates``
        - ``routes/alpha_paint.py:set_templates``
    """

    templates: list[TemplateItem] = Field(default_factory=list, max_length=10)

