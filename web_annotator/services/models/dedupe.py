"""去重模型。

引用: ``services.models`` 包导出；各 routes / services。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DedupeMember(BaseModel):
    """去重组内成员。

    引用:
        ``dedupe.find_duplicate_groups``；``routes.dedupe``。
    """

    path: str
    stem: str
    width: int
    height: int
    blockiness: float
    area: int
    is_keep: bool
    thumbnail_hash: str = ""


class DedupeGroup(BaseModel):
    """一组重复图像。

    引用:
        ``dedupe.find_duplicate_groups``；``routes.dedupe``。
    """

    keep: str
    size: int
    members: list[DedupeMember]
    suggested_remove: list[str]


class DedupeScanResult(BaseModel):
    """去重扫描任务结果。

    引用:
        ``routes.dedupe.scan_duplicates``。
    """

    dirs: list[str]
    dir: str
    image_count: int
    group_count: int
    duplicate_image_count: int
    mse_threshold: float
    similar_res_ratio: float
    groups: list[DedupeGroup]


class DedupeMoveResponse(BaseModel):
    """去重移入回收站回执。

    引用:
        ``routes.dedupe``。
    """

    moved: int
    paths: list[str]
    dirs: list[str]

