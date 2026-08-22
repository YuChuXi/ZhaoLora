"""裁剪页模型. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import ImageThumbItem


class CropMatchConfig(BaseModel):
    """裁剪边长网格配置. 

    引用:
        ``crop_match.match_config``；``routes.crop``. 
    """

    side_min: int
    side_max: int
    side_step: int
    zooms: list[float]
    max_output_pixels: int


class CropImageItem(ImageThumbItem):
    """裁剪页图片列表项.

    引用:
        ``routes.crop.list_crop_images``.
    """

    done: bool


class CropPageConfig(BaseModel):
    """裁剪页配置. 

    引用:
        ``routes.crop.get_config``. 
    """

    parts: list[str]


class CropSavedItem(BaseModel):
    """单次裁剪保存项. 

    引用:
        ``routes.crop.save_crops``. 
    """

    file: str
    size: list[int]
    name: str
    crop: list[int] | None = None


class CropSaveResponse(BaseModel):
    """裁剪保存响应. 

    引用:
        ``routes.crop.save_crops``. 
    """

    saved: list[CropSavedItem]
    count: int

