"""内置来源实现。

引用：导入时自动注册到 ``common._SOURCES``。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from services.booru_client.common import SourceClient, register_source
from services.booru_client.net import (
    UNSUPPORTED_FILE_EXT,
    danbooru_get,
    ext_from_url,
    danbooru_preview_url,
    make_gelbooru,
)
from services.booru_client.utils import find_downloaded


class GelbooruSource(SourceClient):
    name = "gelbooru"
    prefix = "g"
    order_tags = {"score": "sort:score:desc", "id": "sort:id:desc", "id_asc": "sort:id:asc"}
    valid_orders = ("score", "id", "id_asc")

    def filter_extra(self, extra: str) -> str:
        """Gelbooru 忽略 date: 元标签。

        覆盖父类 ``common.SourceClient``；
        引用：``common.SourceClient.build_search_query``。
        """
        tokens = [x for x in extra.split() if not x.lower().startswith("date:")]
        return " ".join(tokens)

    def is_media_url(self, url: str) -> bool:
        """仅允许 gelbooru.com 图床。

        覆盖父类 ``common.SourceClient``；
        引用：``routes/booru.py`` 缩略图代理白名单校验。
        """
        return "gelbooru.com" in url

    @staticmethod
    def normalize(post: Mapping[str, Any]) -> dict | None:
        """Gelbooru 原始帖转标准 dict。

        覆盖父类 ``common.SourceClient``；
        引用：``sources.py:GelbooruSource.search``。
        """
        from services.models import BooruPost
        pid = post.get("id")
        if pid is None:
            return None
        file_url = str(post.get("file_url") or "")
        if not file_url:
            return None
        return BooruPost(
            source="gelbooru", id=str(pid), key=f"gelbooru:{pid}",
            width=int(post.get("width") or 0), height=int(post.get("height") or 0),
            score=int(post.get("score") or 0), tags=str(post.get("tags") or ""),
            preview_url=str(post.get("preview_url") or post.get("sample_url") or file_url),
            sample_url=str(post.get("sample_url") or post.get("preview_url") or file_url),
            file_url=file_url, rating=str(post.get("rating") or ""),
        ).model_dump()

    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """拉取 Gelbooru 帖元数据并写入磁盘。

        实现 ``common.SourceClient.fetch_meta``；
        引用：``common.SourceClient._write_meta`` / ``routes/booru._run_refresh``。
        """
        api = make_gelbooru(proxy)
        post = await api.getPost(int(pid))
        self._write_meta(out_dir, pid, post)

    async def search(self, tags: str, page: int, limit: int, order: str, extra: str,
                     proxy: str | None, **auth) -> tuple[list[dict], bool]:
        """搜索 Gelbooru 帖。

        实现 ``common.SourceClient.search``；
        引用：``common.search_posts`` / ``routes/booru.booru_search``。
        """
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
        out: list[dict] = []
        for p in posts_raw:
            n = self.normalize(p)
            if n is not None:
                out.append(n)
        return out, has_more

    async def download(self, pid: str, out_dir: Path, proxy: str | None, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> dict:
        """下载 Gelbooru 原图并写旁路标签。

        实现 ``common.SourceClient.download``；
        引用：``booru_batch.download_one``。
        """
        from services.booru_client.media import _categorize_gelbooru_tags
        from services.booru_client.utils import convert_path_to_png

        api = make_gelbooru(proxy)
        stem = self.file_stem(pid)
        image_path = out_dir / f"{stem}.png"
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, self.name, pid, file_stem_fn=self.file_stem)

        if not overwrite and existing is not None and tags_path.is_file():
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid}

        post = await api.getPost(int(pid))
        file_url = str(post.get("file_url") or "")
        if not file_url:
            raise ValueError("无 file_url")
        if overwrite or not image_path.is_file():
            tmp_ext = ext_from_url(file_url, ".bin")
            tmp_path = out_dir / f"{stem}.tmp{tmp_ext}"
            await api.getImage(int(pid), str(tmp_path))
            convert_path_to_png(tmp_path, image_path)
            self._clean_old_files(out_dir, pid, image_path)

        if anima_format:
            post = await _categorize_gelbooru_tags(post, api)
        caption = self._build_caption(post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(out_dir, pid, post)
        return {"path": str(image_path), "stem": stem, "id": pid}


class DanbooruSource(SourceClient):
    name = "danbooru"
    prefix = "d"
    order_tags = {"score": "order:score", "id": "order:id_desc", "id_asc": "order:id"}
    valid_orders = ("score", "id", "id_asc")

    def preview_url(self, post: Mapping[str, Any]) -> str:
        """Danbooru 帖缩略图 URL，优先 720x720 webp。

        覆盖父类 ``common.SourceClient``；
        引用：``routes/booru`` 搜索结果列表。
        """
        return danbooru_preview_url(post)

    @staticmethod
    def normalize(post: Mapping[str, Any]) -> dict | None:
        """Danbooru 原始帖转标准 dict。

        覆盖父类 ``common.SourceClient``；
        引用：``sources.py:DanbooruSource.search``。
        """
        from services.models import BooruPost
        if post.get("is_banned") or post.get("is_deleted"):
            return None
        pid = post.get("id")
        file_url = str(post.get("file_url") or "")
        if pid is None or not file_url:
            return None
        return BooruPost(
            source="danbooru", id=str(pid), key=f"danbooru:{pid}",
            width=int(post.get("image_width") or 0), height=int(post.get("image_height") or 0),
            score=int(post.get("score") or 0), tags=str(post.get("tag_string") or ""),
            preview_url=danbooru_preview_url(post),
            sample_url=str(post.get("large_file_url") or post.get("file_url") or ""),
            file_url=file_url, rating=str(post.get("rating") or ""),
            file_ext=str(post.get("file_ext") or ""),
        ).model_dump()

    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """拉取 Danbooru 帖元数据并写入磁盘。

        实现 ``common.SourceClient.fetch_meta``；
        引用：``common.SourceClient._write_meta`` / ``routes/booru._run_refresh``。
        """
        meta = await danbooru_get(
            f"https://danbooru.donmai.us/posts/{pid}.json",
            proxy=proxy, accept="application/json, text/plain, */*", timeout=30.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}")
        self._write_meta(out_dir, pid, meta.json())

    async def search(self, tags: str, page: int, limit: int, order: str, extra: str,
                     proxy: str | None, **auth) -> tuple[list[dict], bool]:
        """搜索 Danbooru 帖。

        实现 ``common.SourceClient.search``；
        引用：``common.search_posts`` / ``routes/booru.booru_search``。
        """
        query = self.build_search_query(tags, order=order, extra=extra)
        limit_used = max(1, min(limit, 100))
        resp = await danbooru_get(
            "https://danbooru.donmai.us/posts.json",
            proxy=proxy, accept="application/json, text/plain, */*", timeout=30.0,
            params={"tags": query, "page": max(1, page), "limit": limit_used},
            login=auth.get("danbooru_login"), api_key=auth.get("danbooru_api_key"),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, list):
            return [], False
        has_more = len(data) >= limit_used
        out: list[dict] = []
        for p in data:
            n = self.normalize(p)
            if n is not None:
                out.append(n)
        return out, has_more

    async def download(self, pid: str, out_dir: Path, proxy: str | None, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> dict:
        """下载 Danbooru 原图并写旁路标签。

        实现 ``common.SourceClient.download``；
        引用：``booru_batch.download_one``。
        """
        from services.booru_client.media import _danbooru_fetch_image_bytes as _fetch

        stem = self.file_stem(pid)
        image_path = out_dir / f"{stem}.png"
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, self.name, pid, file_stem_fn=self.file_stem)

        meta = await danbooru_get(
            f"https://danbooru.donmai.us/posts/{pid}.json",
            proxy=proxy, accept="application/json, text/plain, */*", timeout=60.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}")
        post = meta.json()
        file_ext = str(post.get("file_ext") or "").lower().lstrip(".")
        if file_ext in UNSUPPORTED_FILE_EXT and not post.get("large_file_url"):
            raise ValueError(f"不支持的类型: .{file_ext}")

        if not overwrite and existing is not None and tags_path.is_file():
            self._write_meta(out_dir, pid, post)
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid}

        data = await _fetch(post, proxy=proxy)
        self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        caption = self._build_caption(post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(out_dir, pid, post)
        return {"path": str(image_path), "stem": stem, "id": pid}


register_source(GelbooruSource())
register_source(DanbooruSource())
