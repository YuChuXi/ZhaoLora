"""长任务进度模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/meta.py:`` : 任务取消路由.
    - ``services/core/jobs.py:`` : 任务管理器.
    - ``services/dataset/runtime.py:`` : 运行时快照.
    - ``services/online_sources/batch.py:`` : 批量下载.
    - ``services/state/archiver.py:`` : 归档.
    - ``services/analysis/sam3.py:`` : 各分析任务.
    - ``services/analysis/wd14.py:`` : 各分析任务.
    - ``services/analysis/scoring.py:`` : 各分析任务.
    - ``services/analysis/birefnet.py:`` : 各分析任务.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.archive import ArchivedTask
from services.models.dedupe import DedupeScanResult
from services.models.tags import ConceptQueryResult, EmbeddingQueryResult, TagCleanResult


class AestheticSummary(BaseModel):
    """美学评分结束摘要."""

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
    """提交长任务回执."""

    ok: bool = True
    job_id: str


JobResult = ArchivedTask | DedupeScanResult | TagCleanResult | EmbeddingQueryResult | ConceptQueryResult

class JobSnapshot(BaseModel):
    """长任务进度快照(SSE / 取消回执)."""

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
    """取消长任务回执."""

    ok: bool = True
    status: JobSnapshot

