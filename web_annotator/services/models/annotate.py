"""标注操作回执模型. 

引用: ``services.models`` 包导出；各 routes / services. 
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToggleResponse(BaseModel):
    """切换选中回执. 

    引用:
        ``routes.annotations.toggle``. 
    """

    path: str
    selected: bool


class SelectAllResponse(BaseModel):
    """全选回执. 

    引用:
        ``routes.annotations.select_all``. 
    """

    selected: int
    dirs: list[str]


class CountResponse(BaseModel):
    """计数类回执(取消全选 / 移入回收站 / 恢复). 

    引用:
        ``routes.annotations``. 
    """

    selected: int | None = None
    moved: int | None = None
    restored: int | None = None


class AnnotatorStateResponse(BaseModel):
    """标注状态统计. 

    引用:
        ``routes.annotations.get_state``. 
    """

    dirs: list[str]
    dir: str
    total: int
    selected: int
    trashed: int
    active: int

