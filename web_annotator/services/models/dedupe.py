"""去重模型.

Callers:
    - ``services.models``: 包导出.
    - ``routes.dedupe``: 去重路由.
    - ``services.analysis.dedupe``: 重复图像检测.
    - ``services.models.jobs``: 引用扫描结果类型.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DedupeMember(BaseModel):
    """去重组内成员."""

    path: str
    stem: str
    width: int
    height: int
    blockiness: float
    area: int
    is_keep: bool
    similarity: float | None = None
    thumbnail_hash: str = ""


class DedupeGroup(BaseModel):
    """一组重复图像."""

    keep: str
    size: int
    members: list[DedupeMember]
    suggested_remove: list[str]


class DedupeScanResult(BaseModel):
    """去重扫描任务结果."""

    dirs: list[str]
    dir: str
    image_count: int
    group_count: int
    duplicate_image_count: int
    algorithm: str = "mse"
    mse_threshold: float
    sim_threshold: float = 0.95
    similar_res_ratio: float
    groups: list[DedupeGroup]
    histogram: str | None = None


class DedupeMoveResponse(BaseModel):
    """去重移入回收站回执."""

    moved: int
    paths: list[str]
    dirs: list[str]

