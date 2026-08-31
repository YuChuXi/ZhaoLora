"""online_sources 通用框架: 与具体图源平台无关的基类与工具.

本子包聚合所有来源共用的能力, 供各平台来源实现 (``danbooru`` /
``gelbooru`` / ``bilibili`` / ``x`` / ``pixiv``) 复用:

- ``base``: ``SourceClient`` 抽象基类与来源注册表;
- ``http``: 统一网络层 (基于 httpx);
- ``utils``: 图片识型、文件定位、标签转换等工具;
- ``media``: 下载、取消下载、标签重生成等媒体操作;
- ``gallerydl``: 基于 gallery-dl 的来源客户端基类.

Callers:
    - ``services/online_sources/danbooru/source.py:`` 模块
    - ``services/online_sources/gelbooru/source.py:`` 模块
    - ``routes/online_sources.py:`` 模块
    - ``services/online_sources/x/source.py:`` 模块
    - ``services/online_sources/pixiv/source.py:`` 模块
    - ``routes/__init__.py:`` 模块
"""

from services.online_sources.framework.base import (
    SourceClient,
    build_caption,
    detect_source,
    file_stem,
    get_source,
    list_source_names,
    register_source,
)
from services.online_sources.framework.gallerydl import GalleryDLSource
from services.online_sources.framework.media import (
    download_post,
    local_rel_path,
    regenerate_captions,
    remove_download,
)

__all__ = [
    "GalleryDLSource",
    "SourceClient",
    "build_caption",
    "detect_source",
    "download_post",
    "file_stem",
    "get_source",
    "list_source_names",
    "local_rel_path",
    "regenerate_captions",
    "register_source",
    "remove_download",
]
