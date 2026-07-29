"""就绪 TOML / 蒙版统计模型. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from services.models.dataset import WorkDirInfo


class MaskStats(BaseModel):
    """main 蒙版统计. 

    引用:
        ``services/ready.py:main_mask_stats``. 
    """

    mask_status: Literal["完整", "无"]
    mask_complete: int = 0
    mask_none: int = 0
    main_count: int = 0


class ReadyDirsResponse(BaseModel):
    """就绪页目录列表. 

    引用:
        ``routes.ready.ready_dirs``. 
    """

    root: str
    dirs: list[WorkDirInfo]
    mask_status: Literal["完整", "无"]
    mask_complete: int = 0
    mask_none: int = 0
    main_count: int = 0


class ReadySubset(BaseModel):
    """就绪 TOML 中的一个 subset. 

    引用:
        ``services/ready.py:build_ready_toml``. 
    """

    name: str
    image_dir: str
    num_repeats: int
    image_count: int
    conditioning_data_dir: str | None = None


class ReadyTomlResult(BaseModel):
    """就绪页生成结果. 

    引用:
        ``services/ready.py:build_ready_toml``；``routes/ready.py``. 
    """

    root: str
    toml: str
    subsets: list[ReadySubset]
    dirs: list[WorkDirInfo]
    mask: str | None = None
    num_repeats: int = 1
    mask_status: Literal["完整", "无"] = "无"
    mask_complete: int = 0
    mask_none: int = 0
    main_count: int = 0

