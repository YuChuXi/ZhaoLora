"""本地数据集 / 筛选页模型. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScannedImage(BaseModel):
    """扁平目录扫描得到的一张图片及其旁路文件. 

    引用:
        ``services.scanner.scan_images``. 
    """

    path: str
    stem: str
    suffix: str
    size: int = Field(ge=0)
    siblings: list[str] = Field(default_factory=list)


class WorkDirInfo(BaseModel):
    """可选工作子目录摘要(含图像数量). 

    引用:
        ``work_dir.list_work_dirs``；``wd14_batch.list_image_subdirs``. 
    """

    name: str
    label: str
    image_count: int = Field(0, ge=0)


class AnnotatorState(BaseModel):
    """标注选中与回收站状态. 

    引用:
        ``state_manager.StateManager``. 
    """

    selected: dict[str, int] = Field(default_factory=dict)
    trashed: dict[str, int] = Field(default_factory=dict)


class ImageThumbItem(BaseModel):
    """图片缩略图列表项基类(含路径 / stem / 原始尺寸 / 缩略图 hash).

    引用:
        ``FilterImageItem`` / ``PaintImageItem`` / ``CropImageItem`` / ``TagCheckItem`` 继承.
    """

    path: str
    stem: str
    width: int = 0
    height: int = 0
    thumbnail_hash: str = ""


class FilterImageItem(ImageThumbItem):
    """筛选页图片列表项.

    引用:
        ``routes.images.list_images``.
    """

    suffix: str
    size: int = Field(ge=0)
    siblings: list[str] = Field(default_factory=list)
    selected: bool = False
    trashed: bool = False
    dir: str = ""
    aesthetic: float | None = None
    quality: float | None = None


class SimilarItem(BaseModel):
    """色调相似结果项. 

    引用:
        ``routes.images.similar_images``. 
    """

    path: str
    stem: str
    width: int
    height: int
    selected: bool
    trashed: bool
    thumbnail_hash: str
    distance: float


class SimilarResponse(BaseModel):
    """色调相似接口响应. 

    引用:
        ``routes.images.similar_images``. 
    """

    path: str
    similar: list[SimilarItem] = Field(default_factory=list)


class DirsResponse(BaseModel):
    """工作目录列表响应. 

    引用:
        ``routes.images`` / ``dedupe`` / ``ready`` / ``aesthetic`` / ``tagging``. 
    """

    dirs: list[WorkDirInfo]
    default: str = "main"

