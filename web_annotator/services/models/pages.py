"""评分 / 打标页目录与启动回执.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/scoring.py:`` : 评分路由.
    - ``routes/tagging.py:`` : 打标路由.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from services.models.dataset import WorkDirInfo
from services.models.jobs import JobSnapshot


class ScoringDirsResponse(BaseModel):
    """统一评分页目录与两类分数文件状态."""

    dirs: list[WorkDirInfo]
    aesthetic_ready: bool
    aesthetic_count: int = 0
    quality_ready: bool
    quality_count: int = 0


class ScoringStartResponse(BaseModel):
    """统一评分任务提交回执."""

    ok: bool = True
    job_id: str
    kind: str
    dirs: list[str]


class TaggingDirsResponse(BaseModel):
    """打标页目录与模型状态."""

    dirs: list[WorkDirInfo]
    model_ready: bool
    model_path: str
    default_threshold: float


class TaggingStartResponse(BaseModel):
    """打标任务提交回执."""

    ok: bool = True
    job_id: str
    dirs: list[str]
    threshold: float
    disable_orig_tags: bool
    force: bool
