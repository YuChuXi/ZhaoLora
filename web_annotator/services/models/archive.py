"""归档任务数据模型.

Callers:
    - ``services/models/__init__.py:`` (包导出 ``ArchivedTask``)
    - ``services/state/archiver.py:`` (归档执行产出 ``ArchivedTask``)
    - ``services/models/jobs.py:`` (``JobResult`` 类型联合引用 ``ArchivedTask``)
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ArchivedTask(BaseModel):
    """归档任务执行结果(作为 JobManager 的任务结果载体)."""

    base: str
    name: str
    archived_at: int
    preview_hashes: list[str] = Field(default_factory=list)
    image_count: int = 0
    format: str = "7z"
