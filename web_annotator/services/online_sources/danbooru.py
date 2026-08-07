"""Danbooru 来源客户端: 直连官方 JSON API 搜索与下载.

实现 ``base.SourceClient`` 的 ``search`` / ``download`` / ``fetch_meta``。
API 认证 (login / api_key) 直接从 ``os.environ`` 读取, 不跨层传参。

Callers:
    - ``services.online_sources.__init__`` (注册)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from services.online_sources.framework.base import SourceClient
from services.online_sources.framework.net import (
    UNSUPPORTED_FILE_EXT,
    danbooru_get,
    danbooru_preview_url,
)
from services.online_sources.framework.utils import find_downloaded

if TYPE_CHECKING:
    from services.models import BooruPost


class DanbooruSource(SourceClient):
    name = "danbooru"
    prefix = "d"
    order_tags = {"score": "order:score", "id": "order:id_desc", "id_asc": "order:id"}
    valid_orders = ("score", "id", "id_asc")
    # 每页条数: 上游 posts API 支持的 limit 上限内取固定值
    _PAGE_SIZE = 40

    def preview_url(self, post: BooruPost) -> str:
        """Danbooru 帖缩略图 URL, 优先 720x720 webp.

        覆盖父类 ``base.SourceClient``.

        Callers:
            - `routes/booru.py` 搜索结果列表
        """
        return danbooru_preview_url(post)  # type: ignore[arg-type]

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """Danbooru 原始帖补充 ``source`` 和预览 URL.

        覆盖父类 ``base.SourceClient``.

        Callers:
            - `danbooru.py:DanbooruSource.search`
        """
        if post.is_banned or post.is_deleted or not post.file_url:
            return None
        post.source = "danbooru"
        post.preview_url = danbooru_preview_url(post)
        post.sample_url = post.large_file_url or post.file_url
        return post

    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """拉取 Danbooru 帖元数据并写入磁盘.

        实现 ``base.SourceClient.fetch_meta``.

        Callers:
            - `routes/booru.py:_run_refresh`
        """
        meta = await danbooru_get(
            f"https://danbooru.donmai.us/posts/{pid}.json",
            proxy=proxy, accept="application/json, text/plain, */*", timeout=30.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}")
        self._write_meta(out_dir, pid, BooruPost.model_validate(meta.json()))

    async def search(self, tags: str, page: int, order: str, extra: str,
                     proxy: str | None) -> tuple[list[BooruPost], bool]:
        """搜索 Danbooru 帖.

        每页条数由本来源固定 (``_PAGE_SIZE``), 不随调用方变化。实现
        ``base.SourceClient.search``。

        Callers:
            - `base.py:search_posts` / `routes/booru.py:booru_search`
        """
        from services.models import BooruPost

        query = self.build_search_query(tags, order=order, extra=extra)
        resp = await danbooru_get(
            "https://danbooru.donmai.us/posts.json",
            proxy=proxy, accept="application/json, text/plain, */*", timeout=30.0,
            params={"tags": query, "page": max(1, page), "limit": self._PAGE_SIZE},
            login=os.environ.get("DANBOORU_LOGIN", "").strip() or None,
            api_key=os.environ.get("DANBOORU_API_KEY", "").strip() or None,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, list):
            return [], False
        has_more = len(data) >= self._PAGE_SIZE
        out: list[BooruPost] = []
        for raw_post in data:
            if not isinstance(raw_post, dict):
                continue
            n = self.normalize(BooruPost.model_validate(raw_post))
            if n is not None:
                out.append(n)
        return out, has_more

    async def download(self, pid: str, out_dir: Path, proxy: str | None, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> dict:
        """下载 Danbooru 原图并写旁路标签.

        实现 ``base.SourceClient.download``.

        Callers:
            - ``services/booru.py``
        """
        from services.online_sources.framework.media import _danbooru_fetch_image_bytes as _fetch
        from services.models import BooruPost

        stem = self.file_stem(pid)
        image_path = out_dir / f"{stem}.png"
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, pid, file_stem_fn=self.file_stem)

        meta = await danbooru_get(
            f"https://danbooru.donmai.us/posts/{pid}.json",
            proxy=proxy, accept="application/json, text/plain, */*", timeout=60.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}")
        api_post = BooruPost.model_validate(meta.json())
        if api_post.file_ext in UNSUPPORTED_FILE_EXT and not api_post.large_file_url:
            raise ValueError(f"不支持的类型: .{api_post.file_ext}")

        if not overwrite and existing is not None and tags_path.is_file():
            self._write_meta(out_dir, pid, api_post)
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid}

        data = await _fetch(api_post, proxy=proxy)
        self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        caption = self._build_caption(api_post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(out_dir, pid, api_post)
        return {"path": str(image_path), "stem": stem, "id": pid}