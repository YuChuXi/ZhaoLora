"""Danbooru / Gelbooru 搜索与下载. 

引用: ``routes.booru``. 
"""

from services.booru_client.common import (
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
from services.booru_client.media import (
    download_post,
    fetch_gelbooru_bytes,
    local_rel_path,
    regenerate_captions,
    remove_download,
)
from services.booru_client.sources import DanbooruSource, GelbooruSource
from services.booru_client.utils import is_downloaded

# 注册内置来源
register_source(GelbooruSource())
register_source(DanbooruSource())

__all__ = [
    "META_SUBDIR",
    "SourceClient",
    "build_caption_from_meta",
    "build_search_query",
    "detect_source",
    "download_post",
    "fetch_gelbooru_bytes",
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
