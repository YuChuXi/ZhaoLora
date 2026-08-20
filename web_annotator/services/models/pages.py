"""评分 / 打标页目录与启动回执. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import WorkDirInfo
from services.models.jobs import JobSnapshot


class ScoringDirsResponse(BaseModel):
    """统一评分页目录与两类分数文件状态. 

    引用:
        ``routes.scoring.dirs``. 
    """

    dirs: list[WorkDirInfo]
    aesthetic_ready: bool
    aesthetic_count: int = 0
    quality_ready: bool
    quality_count: int = 0


class ScoringStartResponse(BaseModel):
    """统一评分任务提交回执. 

    引用:
        ``routes.scoring.start``. 
    """

    ok: bool = True
    job_id: str
    kind: str
    dirs: list[str]


class TaggingDirsResponse(BaseModel):
    """打标页目录与模型状态. 

    引用:
        ``routes.tagging.dirs``. 
    """

    dirs: list[WorkDirInfo]
    model_ready: bool
    model_path: str
    default_threshold: float


class TaggingStartResponse(BaseModel):
    """打标任务提交回执. 

    引用:
        ``routes.tagging.start``. 
    """

    ok: bool = True
    job_id: str
    dirs: list[str]
    threshold: float
    disable_orig_tags: bool
    force: bool