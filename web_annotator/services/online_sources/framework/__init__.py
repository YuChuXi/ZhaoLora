"""online_sources 通用框架: 与具体图源平台无关的基类与工具.

本子包聚合所有来源共用的能力, 供各平台来源实现 (``danbooru`` /
``gelbooru`` / ``bilibili`` / ``x`` / ``pixiv``) 复用:

- ``base``: ``SourceClient`` 抽象基类与来源注册表;
- ``net``: 网络请求辅助 (Danbooru/Gelbooru HTTP、代理、URL 派生);
- ``utils``: 图片识型、文件定位、标签转换等工具;
- ``media``: 预览拉取、下载、取消下载、标签重生成等媒体操作;
- ``gallerydl``: 基于 gallery-dl 的来源客户端基类.

Callers:
    - ``services.online_sources.danbooru``
    - ``services.online_sources.gelbooru``
    - ``services.online_sources.bilibili``
    - ``services.online_sources.x``
    - ``services.online_sources.pixiv``
    - ``routes.booru``
"""

from services.online_sources.framework.base import (
    META_SUBDIR,
    SourceClient,
    build_caption_from_meta,
    build_search_query,
    detect_source,
    file_stem,
    get_source,
    list_source_names,
    register_source,
    search_posts,
)
from services.online_sources.framework.gallerydl import GalleryDLSource
from services.online_sources.framework.media import (
    download_post,
    fetch_gelbooru_bytes,
    local_rel_path,
    regenerate_captions,
    remove_download,
)

__all__ = [
    "GalleryDLSource",
    "META_SUBDIR",
    "SourceClient",
    "build_caption_from_meta",
    "build_search_query",
    "detect_source",
    "download_post",
    "fetch_gelbooru_bytes",
    "file_stem",
    "get_source",
    "list_source_names",
    "local_rel_path",
    "regenerate_captions",
    "register_source",
    "remove_download",
    "search_posts",
]