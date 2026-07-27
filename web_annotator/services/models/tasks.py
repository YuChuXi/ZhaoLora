"""任务清单模型。

引用: ``services.models`` 包导出；各 routes / services。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TaskEntry(BaseModel):
    """``.annotator_tasks.json`` 中的单条任务。

    引用:
        ``task_store.TaskStore``。
    """

    completed: bool = False
    created: int = 0
    sort_order: int = 0
    note: str = ""


class TaskListFile(BaseModel):
    """任务清单文件根对象。

    引用:
        ``task_store.TaskStore``。
    """

    tasks: dict[str, TaskEntry] = Field(default_factory=dict)


class TaskInfo(BaseModel):
    """任务操作回执。

    引用:
        ``task_store.add`` / ``set_completed``；``routes.tasks``。
    """

    name: str
    completed: bool
    root: str | None = None
    note: str = ""
    created: int = 0
    sort_order: int = 0


class TaskCard(BaseModel):
    """首页任务卡片。

    引用:
        ``routes.tasks.list_tasks``。
    """

    name: str
    completed: bool
    root: str = ""
    image_count: int = 0
    preview_hashes: list[str] = Field(default_factory=list)
    report_ready: bool = False
    note: str = ""
    created: int = 0
    sort_order: int = 0


class TaskListResponse(BaseModel):
    """任务列表响应。

    引用:
        ``routes.tasks.list_tasks``。
    """

    tasks: list[TaskCard]
    root: str


class TaskCandidatesResponse(BaseModel):
    """可登记任务名候选。

    引用:
        ``routes.tasks.list_candidates``。
    """

    candidates: list[str]


class TaskOkResponse(BaseModel):
    """任务写操作成功回执。

    引用:
        ``routes.tasks``。
    """

    ok: bool = True
    name: str
    completed: bool
    root: str | None = None
    note: str = ""


class SetNoteBody(BaseModel):
    """修改任务备注请求体。引用：``POST /api/tasks/{name}/note``。"""

    note: str = Field(default="", max_length=200)


class DatasetItem(BaseModel):
    """未完成任务（业务页路径校验）。

    引用:
        ``routes.meta.list_datasets``。
    """

    name: str
    root: str


class DatasetsResponse(BaseModel):
    """任务别名列表。

    引用:
        ``routes.meta.list_datasets``。
    """

    datasets: list[DatasetItem]