"""在线图源系统: 各平台来源实现与统一注册表.

聚合 Danbooru / Gelbooru / Bilibili / X / Pixiv 等在线图源, 统一实现
``SourceClient`` 抽象并注册; 对外暴露搜索、下载、缩略图代理等公共 API。
通用框架 (基类、网络、媒体操作) 位于 ``framework`` 子包。

Callers:
    - ``routes/online_sources.py``
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
from services.online_sources.framework.media import (
    download_post,
    local_rel_path,
    regenerate_captions,
    remove_download,
)
from services.online_sources.danbooru import DanbooruSource
from services.online_sources.gelbooru import GelbooruSource
from services.online_sources.bilibili import BilibiliSource
from services.online_sources.x import XSource
from services.online_sources.pixiv import PixivSource, fetch_bytes as fetch_pixiv_bytes
from services.online_sources.framework.utils import is_downloaded

# 注册内置来源
register_source(GelbooruSource())
register_source(DanbooruSource())
register_source(BilibiliSource())
register_source(XSource())
register_source(PixivSource())

__all__ = [
    "META_SUBDIR",
    "SourceClient",
    "build_caption_from_meta",
    "build_search_query",
    "detect_source",
    "download_post",
    "fetch_pixiv_bytes",
    "file_stem",
    "get_source",
    "is_downloaded",
    "list_source_names",
    "local_rel_path",
    "regenerate_captions",
    "register_source",
    "remove_download",
    "search_posts",
]
