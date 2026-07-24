"""Booru 搜索与下载模型。

引用: ``services.models`` 包导出；各 routes / services。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BooruPost(BaseModel):
    """规范化后的图站帖子。

    引用:
        ``booru_client.normalize_*`` / ``search_posts``；``routes.booru``。
    """

    source: str
    id: str
    key: str
    width: int = 0
    height: int = 0
    score: int = 0
    tags: str = ""
    preview_url: str = ""
    sample_url: str = ""
    file_url: str = ""
    rating: str = ""
    file_ext: str = ""
    downloaded: bool = False
    stem: str = ""


class BooruSearchResult(BaseModel):
    """Booru 搜索响应。

    引用:
        ``routes.booru.booru_search``。
    """

    source: str
    tags: str
    extra: str = ""
    order: str = ""
    query: str
    dir: str
    page: int
    limit: int
    count: int
    has_more: bool
    posts: list[BooruPost]


class BooruSourcesResponse(BaseModel):
    """可用图站与代理配置。

    引用:
        ``routes.booru.booru_sources``。
    """

    sources: list[str]
    proxy: str = ""


class BooruDownloadResult(BaseModel):
    """Booru 下载结果。

    引用:
        ``booru_client.download_post``；``routes.booru``。
    """

    path: str
    stem: str
    id: str
    source: str
    tags_path: str = ""
    skipped: bool = False
    dir: str = ""
    rel_path: str = ""
    thumbnail_hash: str = ""


class BooruUndownloadResult(BaseModel):
    """Booru 移除本地下载结果。

    引用:
        ``booru_client.remove_download``；``routes.booru``。
    """

    id: str
    source: str
    removed: list[str] = Field(default_factory=list)
    count: int = 0
    dir: str = ""

