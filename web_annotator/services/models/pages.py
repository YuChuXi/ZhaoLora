"""评分 / 打标页目录与启动回执. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import WorkDirInfo
from services.models.jobs import JobSnapshot


class AestheticDirsResponse(BaseModel):
    """美学页目录与报告状态. 

    引用:
        ``routes.aesthetic.aesthetic_dirs``. 
    """

    root: str
    dirs: list[WorkDirInfo]
    cuda: bool
    default_device: str
    report_ready: bool
    report_url: str | None = None


class AestheticStartResponse(BaseModel):
    """美学任务提交回执. 

    引用:
        ``routes.aesthetic.aesthetic_start``. 
    """

    ok: bool = True
    job_id: str
    dirs: list[str]
    device: str


class AestheticActiveResponse(BaseModel):
    """美学任务重连回执. 

    引用:
        ``routes.aesthetic.aesthetic_active``. 
    """

    ok: bool = True
    job_id: str | None = None
    status: JobSnapshot | None = None


class ScoringDirsResponse(BaseModel):
    """统一评分页目录与两类分数文件状态. 

    引用:
        ``routes.scoring.scoring_dirs``. 
    """

    root: str
    dirs: list[WorkDirInfo]
    cuda: bool
    default_device: str
    aesthetic_ready: bool
    aesthetic_count: int = 0
    quality_ready: bool
    quality_count: int = 0


class ScoringStartResponse(BaseModel):
    """统一评分任务提交回执. 

    引用:
        ``routes.scoring.scoring_start``. 
    """

    ok: bool = True
    job_id: str
    kind: str
    dirs: list[str]
    device: str


class ScoringActiveResponse(BaseModel):
    """统一评分任务重连回执. 

    引用:
        ``routes.scoring.scoring_active``. 
    """

    ok: bool = True
    job_id: str | None = None
    kind: str | None = None
    status: JobSnapshot | None = None


class TaggingDirsResponse(BaseModel):
    """打标页目录与模型状态. 

    引用:
        ``routes.tagging.tagging_dirs``. 
    """

    root: str
    dirs: list[WorkDirInfo]
    model_ready: bool
    model_path: str
    default_threshold: float


class TaggingStartResponse(BaseModel):
    """打标任务提交回执. 

    引用:
        ``routes.tagging.tagging_start``. 
    """

    ok: bool = True
    job_id: str
    dirs: list[str]
    threshold: float
    disable_orig_tags: bool
    force: bool