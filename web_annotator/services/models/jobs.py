"""长任务进度模型. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dedupe import DedupeScanResult
from services.models.tags import TagCleanResult


class AestheticSummary(BaseModel):
    """美学评分结束摘要. 

    引用:
        ``AestheticProgress.summary``；``JobSnapshot.summary``. 
    """

    count: int = 0
    mean: float = 0.0
    median: float = 0.0
    std: float = 0.0
    min: float = 0.0
    max: float = 0.0
    pony_counts: list[int] = Field(default_factory=list)
    scores_file: str = ""
    report: str = ""


class JobSubmitResponse(BaseModel):
    """提交长任务回执. 

    引用:
        aesthetic / tagging / dedupe / tag_stats 启动接口. 
    """

    ok: bool = True
    job_id: str


JobResult = DedupeScanResult | TagCleanResult

class JobSnapshot(BaseModel):
    """长任务进度快照(SSE / 取消回执). 

    引用:
        ``jobs.JobManager``；``aesthetic`` / ``wd14_batch`` snapshot；各长任务路由. 
    """

    status: str
    kind: str = ""
    dataset: str = ""
    job_id: str = ""
    message: str = ""
    done: int = 0
    total: int = 0
    failed: int = 0
    skipped: int = 0
    ok: int = 0
    pct: float | None = None
    phase: str = ""
    current: str = ""
    cancel_requested: bool = False
    report_ready: bool = False
    report_url: str | None = None
    report_mtime: float | None = None
    logs: list[str] = Field(default_factory=list)
    summary: AestheticSummary = Field(default_factory=AestheticSummary)
    result: JobResult | None = None


class JobCancelResponse(BaseModel):
    """取消长任务回执. 

    引用:
        ``routes.meta.job_cancel``. 
    """

    ok: bool = True
    status: JobSnapshot

