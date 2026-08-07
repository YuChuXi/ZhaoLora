"""Gelbooru 来源客户端: 直连官方 API 搜索与下载.

实现 ``base.SourceClient`` 的 ``search`` / ``download`` / ``fetch_meta``。

Callers:
    - ``services.online_sources.__init__`` (注册)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from services.online_sources.framework.base import SourceClient
from services.online_sources.framework.net import (
    ext_from_url,
    make_gelbooru,
)
from services.online_sources.framework.utils import find_downloaded

if TYPE_CHECKING:
    from services.models import BooruPost


class GelbooruSource(SourceClient):
    name = "gelbooru"
    prefix = "g"
    order_tags = {"score": "sort:score:desc", "id": "sort:id:desc", "id_asc": "sort:id:asc"}
    valid_orders = ("score", "id", "id_asc")

    def filter_extra(self, extra: str) -> str:
        """Gelbooru 忽略 date: 元标签, 并合并多评级为 OR.

        Gelbooru 的多评级用 ``(rating:a) OR (rating:b)`` 逐项括号大写
        连接(与 danbooru 的小写 ``or`` 连写不同)。覆盖父类
        ``base.SourceClient``。

        Callers:
            - `base.SourceClient:build_search_query`
        """
        tokens = [x for x in extra.split() if not x.lower().startswith("date:")]
        return " ".join(tokens)

    def is_media_url(self, url: str) -> bool:
        """仅允许 gelbooru.com 图床.

        覆盖父类 ``base.SourceClient``.

        Callers:
            - `routes/booru.py` 缩略图代理白名单检查
        """
        return "gelbooru.com" in url

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """Gelbooru 原始帖补充 ``source`` 和回退 URL.

        覆盖父类 ``base.SourceClient``.

        Callers:
            - `gelbooru.py:GelbooruSource.search`
        """
        if not post.file_url:
            return None
        post.source = "gelbooru"
        post.preview_url = post.preview_url or post.sample_url or post.file_url
        post.sample_url = post.sample_url or post.preview_url
        return post

    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """拉取 Gelbooru 帖元数据并写入磁盘.

        实现 ``base.SourceClient.fetch_meta``.

        Callers:
            - `routes/booru.py:_run_refresh`
        """
        api = make_gelbooru(proxy)
        raw = await api.getPost(int(pid))
        self._write_meta(out_dir, pid, BooruPost.model_validate(raw))

    async def search(self, tags: str, page: int, order: str, extra: str,
                     proxy: str | None) -> tuple[list[BooruPost], bool]:
        """搜索 Gelbooru 帖.

        每页 100 条由上游固定, 页大小不由调用方决定。实现
        ``base.SourceClient.search``。

        Callers:
            - `base.py:search_posts` / `routes/booru.py:booru_search`
        """
        from services.models import BooruPost

        api = make_gelbooru(proxy)
        query = self.build_search_query(tags, order=order, extra=extra)
        pid = max(0, page - 1)
        raw_resp = await api.getJson(f"tags={quote(query, safe='')}&pid={pid}")
        attrs = raw_resp.get("@attributes") if isinstance(raw_resp, dict) else None
        raw = raw_resp.get("post") if isinstance(raw_resp, dict) else None
        posts_raw = [] if raw is None else ([raw] if isinstance(raw, dict) else list(raw))
        g_limit = 100
        offset = pid * g_limit
        if isinstance(attrs, dict):
            g_limit = int(attrs.get("limit") or g_limit)
            offset = int(attrs.get("offset") or offset)
            total = attrs.get("count")
            has_more = offset + len(posts_raw) < int(total) if total is not None else len(posts_raw) >= g_limit
        else:
            has_more = len(posts_raw) >= g_limit
        out: list[BooruPost] = []
        for raw_post in posts_raw:
            if not isinstance(raw_post, dict):
                continue
            n = self.normalize(BooruPost.model_validate(raw_post))
            if n is not None:
                out.append(n)
        return out, has_more

    async def download(self, pid: str, out_dir: Path, proxy: str | None, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> dict:
        """下载 Gelbooru 原图并写旁路标签.

        实现 ``base.SourceClient.download``.

        Callers:
            - ``services/booru.py``
        """
        from services.online_sources.framework.media import _categorize_gelbooru_tags
        from services.online_sources.framework.utils import convert_path_to_png
        from services.models import BooruPost

        api = make_gelbooru(proxy)
        stem = self.file_stem(pid)
        image_path = out_dir / f"{stem}.png"
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, pid, file_stem_fn=self.file_stem)

        if not overwrite and existing is not None and tags_path.is_file():
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid}

        raw = await api.getPost(int(pid))
        api_post = BooruPost.model_validate(raw)
        if not api_post.file_url:
            raise ValueError("无 file_url")
        if overwrite or not image_path.is_file():
            tmp_ext = ext_from_url(api_post.file_url, ".bin")
            tmp_path = out_dir / f"{stem}.tmp{tmp_ext}"
            await api.getImage(int(pid), str(tmp_path))
            convert_path_to_png(tmp_path, image_path)
            self._clean_old_files(out_dir, pid, image_path)

        if anima_format:
            raw = await _categorize_gelbooru_tags(raw, api)
            api_post = BooruPost.model_validate(raw)
        caption = self._build_caption(api_post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(out_dir, pid, api_post)
        return {"path": str(image_path), "stem": stem, "id": pid}