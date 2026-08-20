"""归档任务数据模型.

引用: ``services.models`` 包导出; ``routes.tasks``; ``services.state.archiver``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ArchivedTask(BaseModel):
    """已归档任务元数据.

    存储于 ``__packed__/{base}.json``, 与归档文件 ``{base}.7z`` 同名前缀.

    引用:
        ``services.state.archiver``; ``routes.tasks``.
    """

    base: str
    name: str
    archived_at: int
    archive_file: str
    preview_hashes: list[str] = Field(default_factory=list)
    image_count: int = 0
    format: str = "7z"


class ArchivedListResponse(BaseModel):
    """归档任务列表响应.

    引用:
        ``routes.tasks.list_archived``.
    """

    archives: list[ArchivedTask]
