"""就绪 TOML / 蒙版统计模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/ready.py:`` : 就绪页路由.
    - ``services/ready.py:`` : 就绪 TOML 构建.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from services.models.dataset import WorkDirInfo

# 训练模式: style=画风(network rank 16) / character=角色(network rank 32)
LoraMode = Literal["style", "character"]


class MaskStats(BaseModel):
    """单目录就绪统计: 图片 / 标注 / 蒙版计数与就绪状态.

    ``text_count`` 与 ``mask_complete`` 仅统计有对应图片的条目;
    ``ready`` 表示标注全部存在, 且蒙版全部存在或全无.
    """

    name: str = "main"
    mask_status: Literal["完整", "无"]
    main_count: int = 0
    text_count: int = 0
    mask_complete: int = 0
    mask_none: int = 0
    ready: bool = False


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
    """就绪页生成结果: 子集/蒙版统计与训练配置预览文本(文件集合随导出格式变化)."""

    root: str
    format: str = Field(description="本次生成的配置导出格式(即 assets/configs/ 下的模板目录名)")
    files: dict[str, str] = Field(default_factory=dict, description="配置文件预览文本, 键为该格式的全部配置文件名(由模板目录扫描得到), 文本即导出写入的内容")
    subsets: list[ReadySubset]
    dirs: list[WorkDirInfo]
    mask_stats: list[MaskStats] = Field(default_factory=list)
    mask: str | None = None
    target_images: int = Field(512, ge=1, description="目标图片数, 据此计算各子集各分辨率块 num_repeats 使每个子集重复后图片数 >= 目标折算份额")
    subset_repeats: dict[str, list[int]] = Field(default_factory=dict, description="各子集在每个分辨率上的 num_repeats(键为子集名, 列表与其分辨率列表一一对应)")
    mask_status: Literal["完整", "无"] = "无"
    mask_complete: int = 0
    mask_none: int = 0
    main_count: int = 0


class TrainExportResult(BaseModel):
    """就绪页导出整套训练配置的结果(仅有文件名, 不向前端暴露导出绝对路径).

    Callers:
        - ``routes/ready.py:export``
    """

    files: list[str] = Field(default_factory=list, description="写入的配置文件文件名")

