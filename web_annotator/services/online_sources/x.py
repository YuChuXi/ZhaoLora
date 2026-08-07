"""X(Twitter) 来源客户端: 用 gallery-dl 驱动, 按用户名拉取其推文图片.

``tags`` 参数承载 X 用户名, ``extra`` 暂不支持筛选。搜索采用**真增量**:
经 ``GalleryDLSource`` 每批消费到 ``_INCR_BATCH`` 张图片后 ``break`` 并保存
游标到缓存; 后续请求页码超出缓存时, 用 ``cursor`` 配置续拉, 缓存按需增长。
过滤纯图片 (type=photo) 展开为扁平卡片列表并按序号切片返回。

下载时按 ``{username}_{tweet_id}_{num}`` 的 pid 下载整条推文媒体, 取第
``num`` 张转为 PNG 落盘。

访问 X 需要登录 cookies, 从 ``os.environ["X_COOKIES"]`` 读取 (Netscape
格式, 由 ``server.py:_load_env_config`` 解析为绝对路径后写入).

Callers:
    - ``services.online_sources.__init__`` (注册)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from services.online_sources.framework.gallerydl import GalleryDLSource
from services.models import BooruPost

# 单批增量拉取的图片数 (超出请求页时按此粒度续拉)
_INCR_BATCH = 200
# 仅保留的媒体类型
_IMAGE_TYPES = frozenset({"photo"})
# 图片扩展名 (无前导点, 用于从下载结果中定位媒体)
_IMG_EXT = frozenset({"jpg", "jpeg", "png", "webp"})


def _sized_url(url: str, size: str) -> str:
    """将 X 图片 URL 的尺寸参数替换为指定档位.

    gallery-dl 产出的 URL 形如 ``...?format=jpg&name=4096x4096``, 把
    ``name=`` 后档位换成 ``size``; 无尺寸参数时追加. X 支持 ``small`` /
    ``medium`` / ``large`` / ``4096x4096`` 等档位, 用于缩略图与样图.

    Args:
        url: 原始媒体 URL.
        size: 目标尺寸档位.

    Returns:
        str: 替换尺寸后的 URL.

    Callers:
        - `XSource._post_from_item`
    """
    for sep in ("&name=", "?name="):
        if sep in url:
            base, _, _ = url.rpartition(sep)
            return base + sep + size
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}format=jpg&name={size}"


class XSource(GalleryDLSource):
    """从 X 用户 timeline 拉取图片的来源客户端.

    Callers:
        - ``__init__.py:register_source``
    """

    name = "x"
    prefix = "x"
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()
    _IMG_EXT = _IMG_EXT
    _INCR_BATCH = _INCR_BATCH

    def _auth_overrides(self) -> list[tuple[tuple[str, ...], str, Any]]:
        """构造登录 cookies 的临时配置项, 供 ``config.apply`` 使用.

        Returns:
            list[tuple]: 形如 ``[((), "cookies", 路径)]`` 的配置项列表;
            未配置 cookies 时为空列表.

        Callers:
            - `GalleryDLSource._gallery_meta_incremental`
            - `GalleryDLSource._download_to_dir`
        """
        cookies = os.environ.get("X_COOKIES", "").strip()
        return [] if not cookies else [((), "cookies", cookies)]

    def _search_key(self, tags: str, extra: str) -> str | None:
        """把查询参数归一为用户名字符串缓存键.

        Args:
            tags: X 用户名.
            extra: 附加筛选串 (暂不支持).

        Returns:
            str | None: 用户名; 为空时返回 None.

        Callers:
            - `GalleryDLSource.search`
        """
        u = (tags or "").strip()
        return u if u else None

    def _search_url(self, key: str) -> str:
        """由用户名构造 X 用户媒体页 URL.

        Args:
            key: 用户名.

        Returns:
            str: 用户媒体页 URL.

        Callers:
            - `GalleryDLSource._ensure_cached`
        """
        return f"https://x.com/{key}/media"

    def _split_pid(self, pid: str) -> tuple[str, str, int]:
        """将 ``{username}_{tweet_id}_{num}`` 拆为 (用户名, 推文ID, 序号).

        Args:
            pid: 卡片 id.

        Returns:
            tuple[str, str, int]: (username, tweet_id, num).

        Raises:
            ValueError: pid 格式非法.

        Callers:
            - `GalleryDLSource.download`
            - `GalleryDLSource.fetch_meta`
        """
        parts = pid.rsplit("_", 2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise ValueError(f"非法 id: {pid}")
        return parts[0], parts[1], int(parts[2])

    def _detail_url(self, parts: tuple[str, str, int]) -> str:
        """由拆分后的 pid 构造推文页 URL.

        status URL 需带用户名前缀, 否则 gallery-dl 无法匹配 extractor.

        Args:
            parts: ``(username, tweet_id, num)``.

        Returns:
            str: 推文页 URL.

        Callers:
            - `GalleryDLSource._fetch_detail`
            - `GalleryDLSource.download`
        """
        return f"https://x.com/{parts[0]}/status/{parts[1]}"

    def _post_from_item(self, url: str, kwdict: dict, key: Any = None) -> BooruPost | None:
        """将 gallery-dl 单条媒体元数据转为 BooruPost.

        仅保留纯图片; pid 用 ``{username}_{tweet_id}_{num}`` (num 为 1-based
        推文内媒体序号, username 来自搜索缓存键) 自包含下载定位所需信息.

        Args:
            url: 媒体 URL.
            kwdict: gallery-dl 产出的媒体元数据字典.
            key: 搜索缓存键 (用户名); 无上下文时为 None (id 随后被覆盖).

        Returns:
            BooruPost | None: 非图片或无有效 URL 时返回 None.

        Callers:
            - `GalleryDLSource._ensure_cached`
            - `GalleryDLSource.fetch_meta`
        """
        if kwdict.get("type") not in _IMAGE_TYPES:
            return None
        tweet_id = kwdict.get("tweet_id")
        num = kwdict.get("num")
        if not url or not tweet_id or not num:
            return None
        username = key or ""
        return BooruPost(
            source=self.name,
            id=f"{username}_{tweet_id}_{num}",
            width=int(kwdict.get("width") or 0),
            height=int(kwdict.get("height") or 0),
            file_url=url,
            preview_url=_sized_url(url, "small"),
            sample_url=url,
            tags=(kwdict.get("content") or "").strip(),
        )

    def _caption_from_item(self, kwdict: dict) -> str:
        """从媒体元数据提取推文正文文本 (压缩空白).

        Args:
            kwdict: gallery-dl 产出的媒体元数据字典.

        Returns:
            str: 推文正文文本.

        Callers:
            - `GalleryDLSource.download`
        """
        return re.sub(r"\s+", " ", (kwdict.get("content") or "")).strip()