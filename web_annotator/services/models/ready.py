"""就绪 TOML / 蒙版统计模型.

Callers:
    - ``services.models``: 包导出.
    - ``routes.ready``: 就绪页路由.
    - ``services.ready``: 就绪 TOML 构建.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from services.models.dataset import WorkDirInfo


class MaskStats(BaseModel):
    """单目录蒙版统计."""

    name: str = "main"
    mask_status: Literal["完整", "无"]
    mask_complete: int = 0
    mask_none: int = 0
    main_count: int = 0


class ReadyDirsResponse(BaseModel):
    """就绪页目录列表."""

    root: str
    dirs: list[WorkDirInfo]
    mask_stats: list[MaskStats] = Field(default_factory=list)
    mask_status: Literal["完整", "无"] = "无"
    mask_complete: int = 0
    mask_none: int = 0
    main_count: int = 0


class ReadySubset(BaseModel):
    """就绪 TOML 中的一个 subset."""

    name: str
    image_dir: str
    image_count: int
    conditioning_data_dir: str | None = None


class ReadyTomlResult(BaseModel):
    """就绪页生成结果."""

    root: str
    toml: str
    subsets: list[ReadySubset]
    dirs: list[WorkDirInfo]
    mask_stats: list[MaskStats] = Field(default_factory=list)
    mask: str | None = None
    target_images: int = Field(512, ge=1, description="目标图片数, 据此计算各分辨率块 num_repeats 使跨所有块总重复图片数 >= 目标")
    repeats: list[int] = Field(default_factory=list, description="每个分辨率块的 num_repeats(按 resolutions 顺序); 单块时为单元素")
    resolutions: list[int] = Field(default_factory=list, description="多分辨率列表, 空表示单块无 resolution")
    mask_status: Literal["完整", "无"] = "无"
    mask_complete: int = 0
    mask_none: int = 0
    main_count: int = 0

