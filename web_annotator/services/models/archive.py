"""归档任务数据模型.

Callers:
    - ``services/models/__init__.py:`` (包导出 ``ArchivedTask`` / ``ArchivedListResponse``)
    - ``services/state/archiver.py:`` (归档执行读写 ``ArchivedTask``)
    - ``services/models/jobs.py:`` (``JobResult`` 类型联合引用 ``ArchivedTask``)
    - ``routes/tasks.py:list_archived`` (返回 ``ArchivedListResponse``)
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ArchivedTask(BaseModel):
    """已归档任务元数据.

    存储于 ``__packed__/{base}.json``, 与归档文件 ``{base}.7z`` 同名前缀.
    """

    base: str
    name: str
    archived_at: int
    archive_file: str
    preview_hashes: list[str] = Field(default_factory=list)
    image_count: int = 0
    format: str = "7z"


class ArchivedListResponse(BaseModel):
    """归档任务列表响应."""

    archives: list[ArchivedTask]
