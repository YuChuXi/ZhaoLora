"""标注操作回执模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/annotations.py:`` : 标注路由.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToggleResponse(BaseModel):
    """切换选中回执."""

    path: str
    selected: bool


class SelectAllResponse(BaseModel):
    """全选回执."""

    selected: int
    dirs: list[str]


class CountResponse(BaseModel):
    """计数类回执(取消全选 / 移入回收站 / 恢复)."""

    selected: int | None = None
    moved: int | None = None
    restored: int | None = None


class AnnotatorStateResponse(BaseModel):
    """标注状态统计."""

    dirs: list[str]
    dir: str
    total: int
    selected: int
    trashed: int
    active: int

