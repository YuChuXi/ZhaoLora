"""文档页模型。

引用: ``services.models`` 包导出；各 routes / services。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocNavItem(BaseModel):
    """文档导航项。

    引用:
        ``routes.docs.docs_nav``。
    """

    slug: str
    label: str


class DocsListResponse(BaseModel):
    """文档导航列表。

    引用:
        ``routes.docs.list_docs``。
    """

    nav: list[DocNavItem]


class DocContentResponse(BaseModel):
    """单篇文档内容。

    引用:
        ``routes.docs.get_doc``。
    """

    slug: str
    file: str
    title: str
    html: str

