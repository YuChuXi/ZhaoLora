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


class TxtDirInfo(BaseModel):
    """含旁路 ``.txt`` 的子目录摘要. 

    引用:
        ``tag_stats.list_txt_subdirs``. 
    """

    name: str
    label: str
    txt_count: int = Field(0, ge=0)


class AnnotatorState(BaseModel):
    """标注选中与回收站状态. 

    引用:
        ``state_manager.StateManager``. 
    """

    selected: dict[str, int] = Field(default_factory=dict)
    trashed: dict[str, int] = Field(default_factory=dict)


class ImageListItem(BaseModel):
    """筛选页图片列表项. 

    引用:
        ``routes.images.list_images``. 
    """

    path: str
    stem: str
    suffix: str
    size: int = Field(ge=0)
    siblings: list[str] = Field(default_factory=list)
    width: int = 0
    height: int = 0
    selected: bool = False
    trashed: bool = False
    thumbnail_hash: str = ""
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

    root: str
    dirs: list[WorkDirInfo]
    default: str = "main"

