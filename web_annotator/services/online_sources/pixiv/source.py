"""pixiv 来源适配器: 通过 pixiv web AJAX API 按画师或标签拉取作品图片.

``tags`` 参数承载画师 ID 或标签词, ``extra`` 承载模式标识
(``mode:user``=按画师, ``mode:tag``=按标签)。按画师搜索时全量拉取
作品列表并展开为扁平卡片, 按标签搜索时分页拉取搜索结果, 缓存于进程内
后按页切片返回。

下载时按 ``{illust_id}_{num}`` 的 pid 下载作品, 取第 ``num``
张从 pximg 图床拉取并转为 PNG 落盘。

Callers:
    - ``pixiv/__init__.py`` (导出)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from services.online_sources.framework import http
from services.online_sources.framework.base import SourceClient
from services.online_sources.framework.utils import find_downloaded
from services.models import BooruPost
from services.online_sources.pixiv.client import fetch_bytes, request_headers

# 每页卡片数
_PAGE_SIZE = 40

# 仅保留的媒体类型 (0=illust, 1=manga; 排除 2=ugoira)
_IMG_TYPES = frozenset({0, 1})


class PixivSource(SourceClient):
    """从 pixiv 画师或标签拉取图片的来源客户端.

    Callers:
        - ``__init__.py:register_source``
    """

    name = "pixiv"
    prefix = "p"
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()
    image_hosts = ("pximg.net",)
    image_referer = "https://www.pixiv.net/"

    def __init__(self) -> None:
        """初始化每查询缓存与并发锁。"""
        super().__init__()
        self._cache: dict[tuple[str, str], list[BooruPost]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def parse_query(self, tags: str, extra: str) -> dict[str, str]:
        """解析 pixiv 查询串: ``tags`` 为值, ``extra`` 里 ``mode:`` 指定模式.

        Args:
            tags: 画师 ID 或标签词。
            extra: ``mode:user``(画师) 或 ``mode:tag``(标签), 缺省按画师.

        Returns:
            dict[str, str]: 含 ``mode`` 与 ``value`` 两键。

        Callers:
            - 前端 booru_filter.js
        """
        mode = "user"
        for token in (extra or "").split():
            if token.startswith("mode:"):
                val = token[len("mode:"):].strip()
                if val in ("user", "tag"):
                    mode = val
        return {"mode": mode, "value": str(tags).strip()}

    # ---- web AJAX API ----

    async def _request(self, url: str) -> dict[str, Any]:
        """调用 pixiv web AJAX API, 返回 JSON 响应体.

        Args:
            url: API 请求 URL.

        Returns:
            dict: API 响应 JSON.

        Raises:
            http.NetworkError: HTTP 错误。
            RuntimeError: API 返回 ``error: true``。

        Callers:
            - `PixivSource._search_user`
            - `PixivSource._search_tag_page`
            - `PixivSource._fetch_pages`
            - `PixivSource._fetch_detail`
        """
        resp = await http.request(url, headers=request_headers())
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(data.get("message", "pixiv API 错误"))
        return data

    @staticmethod
    def _extract_tags(item: dict) -> str:
        """从 illust 数据中提取标签文本 (空格连接).

        Args:
            item: illust 数据字典。

        Returns:
            str: 标签文本。

        Callers:
            - `PixivSource._search_user`
            - `PixivSource._search_tag_page`
        """
        tags = item.get("tags", [])
        if isinstance(tags, dict):
            tags = tags.get("tags", [])
        if not isinstance(tags, list) or not tags:
            return ""
        if isinstance(tags[0], dict):
            return " ".join(t.get("tag", "") for t in tags)
        return " ".join(str(t) for t in tags)

    @staticmethod
    def _to_posts(illust_id: str, item: dict, urls: dict) -> list[BooruPost]:
        """将单幅作品按页数展开为扁平卡片列表.

        Args:
            illust_id: 作品 ID。
            item: illust 数据字典。
            urls: 图片 URL 字典, 含 ``regular`` 键.

        Returns:
            list[BooruPost]: 卡片列表.

        Callers:
            - `PixivSource._search_user`
            - `PixivSource._search_tag_page`
        """
        page_count = item.get("pageCount", 1)
        sample_url = urls.get("regular", "")
        tags = PixivSource._extract_tags(item)
        posts: list[BooruPost] = []
        for num in range(page_count):
            posts.append(BooruPost(
                source="pixiv",
                id=f"{illust_id}_{num}",
                width=item.get("width", 0),
                height=item.get("height", 0),
                file_url=sample_url,
                preview_url=sample_url,
                sample_url=sample_url,
                tags=tags,
            ))
        return posts

    # ---- 搜索 ----

    async def _search_user(self, user_id: str) -> list[BooruPost]:
        """拉取指定画师的全部作品, 展开为扁平卡片列表.

        分两步: (1) 用 ``profile/all`` 获取全部作品 ID, (2) 逐幅
        拉取作品详情后按多页展开。detail 请求以 5 并发数分批调用,
        控制频率。

        Args:
            user_id: 画师 ID。

        Returns:
            list[BooruPost]: 按 ID 降序排列的卡片列表。

        Callers:
            - `PixivSource.search`
        """
        data = await self._request(
            f"https://www.pixiv.net/ajax/user/{user_id}/profile/all",
        )
        body = data.get("body", {})
        illust_ids = [int(k) for k in body.get("illusts", {})]
        manga_ids = [int(k) for k in body.get("manga", [])]
        all_ids = sorted(illust_ids + manga_ids, reverse=True)

        posts: list[BooruPost] = []
        sem = asyncio.Semaphore(5)
        batch_size = 20

        async def _fetch_one(iid: int) -> None:
            async with sem:
                detail = await self._request(
                    f"https://www.pixiv.net/ajax/illust/{iid}",
                )
            item = detail.get("body", {})
            if item.get("illustType") not in _IMG_TYPES:
                return
            posts.extend(self._to_posts(str(iid), item, item.get("urls", {})))

        for i in range(0, len(all_ids), batch_size):
            batch = all_ids[i:i + batch_size]
            await asyncio.gather(*[_fetch_one(iid) for iid in batch])

        return posts

    async def _search_tag_page(
        self, tag: str, page: int,
    ) -> tuple[list[BooruPost], int]:
        """拉取一页标签搜索结果, 展开为扁平卡片列表.

        搜索结果中每项不含 ``urls`` 字典, 只有方形缩略图 ``url``,
        预处理为 regular URL 再展开.

        Args:
            tag: 标签词。
            page: 页码 (1-based)。

        Returns:
            tuple[list[BooruPost], int]: (卡片列表, 总结果数).

        Callers:
            - `PixivSource.search`
        """
        data = await self._request(
            f"https://www.pixiv.net/ajax/search/illustrations/{tag}"
            f"?word={tag}&order=date_d&mode=all&p={page}"
            f"&s_mode=s_tag_full&type=illust_and_ugoira&lang=zh",
        )
        illust = data.get("body", {}).get("illust", {})
        items = illust.get("data", [])
        total = illust.get("total", 0)
        posts: list[BooruPost] = []
        for item in items:
            if item.get("illustType") not in _IMG_TYPES:
                continue
            posts.extend(self._to_posts(
                str(item["id"]),
                item,
                {"regular": item.get("url", "")},
            ))
        return posts, total

    async def search(
        self,
        tags: str,
        page: int,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool]:
        """按查询键搜索, 返回 (posts, has_more)。

        按画师时全量拉取后缓存; 按标签时逐页拉取搜索结果并缓存,
        页大小固定为 ``_PAGE_SIZE``。

        Callers:
            - ``routes/online_sources.py:search``
        """
        f = self.parse_query(tags, extra)
        value = f["value"]
        if not value:
            return [], False
        key = (f["mode"], value)

        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock

        async with lock:
            cache = self._cache.get(key)
            if cache is None:
                if f["mode"] == "user":
                    cache = await self._search_user(value)
                else:
                    cache, _ = await self._search_tag_page(value, 1)
                self._cache[key] = cache

        posts = cache
        start = (page - 1) * _PAGE_SIZE
        slice_posts = posts[start : start + _PAGE_SIZE]
        has_more = start + _PAGE_SIZE < len(posts)
        return slice_posts, has_more

    # ---- 下载 / 元数据 ----

    async def _fetch_pages(self, illust_id: str) -> list[dict]:
        """获取作品的所有分页 URL.

        Args:
            illust_id: 作品 ID。

        Returns:
            list[dict]: 分页列表, 每项含 ``urls.original``。

        Callers:
            - `PixivSource.download`
        """
        data = await self._request(
            f"https://www.pixiv.net/ajax/illust/{illust_id}/pages",
        )
        return data.get("body", [])

    async def _fetch_detail(self, illust_id: str) -> dict:
        """获取作品详情 (标签、尺寸等).

        Args:
            illust_id: 作品 ID。

        Returns:
            dict: 作品详情。

        Callers:
            - `PixivSource.download`
            - `PixivSource.fetch_meta`
        """
        data = await self._request(
            f"https://www.pixiv.net/ajax/illust/{illust_id}",
        )
        return data.get("body", {})

    async def download(
        self,
        pid: str,
        out_dir: Path,
        *,
        overwrite: bool = False,
        anima_format: bool = True,
        include_meta_tags: bool = False,
        keep_underscores: bool = False,
    ) -> dict:
        """下载指定媒体为 PNG, 并写旁路标签与 meta.

        Callers:
            - ``base.py:SourceClient.download``
        """
        illust_id, num = self._split_pid(pid)

        stem = self.file_stem(pid)
        image_path = out_dir / f"{stem}.png"
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, pid, file_stem_fn=self.file_stem)

        if not overwrite and existing is not None and tags_path.is_file():
            return {
                "skipped": True,
                "path": str(existing),
                "stem": stem,
                "id": pid,
                "tags_path": str(tags_path),
            }

        pages = await self._fetch_pages(illust_id)
        if num >= len(pages):
            raise ValueError(f"pid {pid} 无第 {num} 张媒体")
        target_url = pages[num].get("urls", {}).get("original", "")

        data, _ = await fetch_bytes(target_url)

        self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)

        detail = await self._fetch_detail(illust_id)
        caption = self._extract_tags(detail)
        if include_meta_tags:
            caption = (caption + (" " + self.name) if caption else self.name).strip()
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(
            out_dir,
            pid,
            BooruPost(source=self.name, id=pid, file_url=target_url, tags=caption),
        )
        return {
            "path": str(image_path),
            "stem": stem,
            "id": pid,
            "tags_path": str(tags_path),
        }

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """按 pid 拉取媒体元数据并写入磁盘.

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
        illust_id, num = self._split_pid(pid)
        detail = await self._fetch_detail(illust_id)
        page_count = detail.get("pageCount", 1)
        if num >= page_count:
            raise ValueError(f"pid {pid} 无第 {num} 张媒体")
        post = BooruPost(
            source=self.name,
            id=pid,
            file_url=detail.get("urls", {}).get("regular", ""),
            tags=self._extract_tags(detail),
        )
        self._write_meta(out_dir, pid, post)

    @staticmethod
    def _split_pid(pid: str) -> tuple[str, int]:
        """将 ``{illust_id}_{num}`` 拆为 (作品ID, 页序号).

        Args:
            pid: 卡片 id。

        Returns:
            tuple[str, int]: (illust_id, num)。

        Raises:
            ValueError: pid 格式非法。

        Callers:
            - `PixivSource.download`
            - `PixivSource.fetch_meta`
        """
        idx = pid.rfind("_")
        if idx <= 0 or not pid[idx + 1:].isdigit():
            raise ValueError(f"非法 id: {pid}")
        return pid[:idx], int(pid[idx + 1:])