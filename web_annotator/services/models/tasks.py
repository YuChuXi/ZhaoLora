"""任务清单模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/tasks.py:`` : 任务路由.
    - ``services/state/tasks.py:`` : 任务存储.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TaskEntry(BaseModel):
    """``.annotator_tasks.json`` 中的单条任务."""

    name: str = ""
    completed: bool = False
    created: int = 0
    sort_order: int = 0
    note: str = ""
    trigger_word: str = ""
    # 归档状态与归档详情(压缩包/预览位于 ``__packed__/{archive_base}.*``)
    archived: bool = False
    archived_at: int | None = None
    archive_base: str = ""
    preview_hashes: list[str] = Field(default_factory=list)
    image_count: int = 0


class TaskListFile(BaseModel):
    """任务清单文件根对象."""

    tasks: list[TaskEntry] = Field(default_factory=list)


class TaskInfo(BaseModel):
    """任务操作回执."""

    name: str
    completed: bool
    root: str | None = None
    note: str = ""
    trigger_word: str = ""
    created: int = 0
    sort_order: int = 0
    archived: bool = False
    archive_base: str = ""
    archived_at: int | None = None
    image_count: int = 0
    preview_hashes: list[str] = Field(default_factory=list)


class TaskCard(BaseModel):
    """首页/归档任务卡片."""

    name: str
    completed: bool
    image_count: int = 0
    preview_hashes: list[str] = Field(default_factory=list)
    report_ready: bool = False
    note: str = ""
    trigger_word: str = ""
    created: int = 0
    sort_order: int = 0
    # 归档状态信息(archived 任务时有效, 用于预览/恢复)
    archived: bool = False
    archive_base: str = ""
    archived_at: int | None = None


class TaskListResponse(BaseModel):
    """任务列表响应."""

    tasks: list[TaskCard]


class TaskCandidatesResponse(BaseModel):
    """可登记任务名候选."""

    candidates: list[str]


class TaskOkResponse(BaseModel):
    """任务写操作成功回执."""

    ok: bool = True
    name: str
    completed: bool
    note: str = ""
    trigger_word: str = ""


class SetNoteBody(BaseModel):
    """修改任务备注请求体."""

    note: str = ""


class SetTriggerWordBody(BaseModel):
    """修改任务触发词请求体."""

    trigger_word: str = ""


class DatasetItem(BaseModel):
    """未完成任务(业务页路径校验)."""

    name: str


class DatasetsResponse(BaseModel):
    """任务别名列表."""

    datasets: list[DatasetItem]
