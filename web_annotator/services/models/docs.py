"""文档页模型.

Callers:
    - ``services/models/__init__.py:`` : 包导出.
    - ``routes/docs.py:`` : 文档路由.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocNavItem(BaseModel):
    """文档导航项."""

    slug: str
    label: str


class DocsListResponse(BaseModel):
    """文档导航列表."""

    nav: list[DocNavItem]


class DocContentResponse(BaseModel):
    """单篇文档内容."""

    slug: str
    file: str
    title: str
    html: str

