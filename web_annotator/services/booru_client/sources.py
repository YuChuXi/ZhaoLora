"""内置来源实现. 

Callers:
    - ``__init__.py`` 导入时自动注册
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from services.booru_client.common import SourceClient
from services.booru_client.net import (
    UNSUPPORTED_FILE_EXT,
    danbooru_get,
    ext_from_url,
    danbooru_preview_url,
    make_gelbooru,
)
from services.booru_client.utils import find_downloaded

if TYPE_CHECKING:
    from services.models import BooruPost


class GelbooruSource(SourceClient):
    name = "gelbooru"
    prefix = "g"
    order_tags = {"score": "sort:score:desc", "id": "sort:id:desc", "id_asc": "sort:id:asc"}
    valid_orders = ("score", "id", "id_asc")

    def filter_extra(self, extra: str) -> str:
        """Gelbooru 忽略 date: 元标签. 

        覆盖父类 ``common.SourceClient``. 

        Callers:
            - `common.SourceClient:build_search_query`
        """
        tokens = [x for x in extra.split() if not x.lower().startswith("date:")]
        return " ".join(tokens)

    def is_media_url(self, url: str) -> bool:
        """仅允许 gelbooru.com 图床. 

        覆盖父类 ``common.SourceClient``. 

        Callers:
            - `routes/booru.py` 缩略图代理白名单检查
        """
        return "gelbooru.com" in url

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """Gelbooru 原始帖补充 ``source`` 和回退 URL. 

        覆盖父类 ``common.SourceClient``. 

        Callers:
            - `sources.py:GelbooruSource.search`
        """
        if not post.file_url:
            return None
        post.source = "gelbooru"
        post.preview_url = post.preview_url or post.sample_url or post.file_url
        post.sample_url = post.sample_url or post.preview_url
        return post

    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """拉取 Gelbooru 帖元数据并写入磁盘. 

        实现 ``common.SourceClient.fetch_meta``. 

        Callers:
            - `routes/booru.py:_run_refresh`
        """
        api = make_gelbooru(proxy)
        raw = await api.getPost(int(pid))
        self._write_meta(out_dir, pid, BooruPost.model_validate(raw))

    async def search(self, tags: str, page: int, limit: int, order: str, extra: str,
                     proxy: str | None, **auth) -> tuple[list[BooruPost], bool]:
        """搜索 Gelbooru 帖. 

        实现 ``common.SourceClient.search``. 

        Callers:
            - `common.py:search_posts` / `routes/booru.py:booru_search`
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

        实现 ``common.SourceClient.download``. 

        Callers:
            - ``services/booru.py``
        """
        from services.booru_client.media import _categorize_gelbooru_tags
        from services.booru_client.utils import convert_path_to_png
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


class DanbooruSource(SourceClient):
    name = "danbooru"
    prefix = "d"
    order_tags = {"score": "order:score", "id": "order:id_desc", "id_asc": "order:id"}
    valid_orders = ("score", "id", "id_asc")

    def preview_url(self, post: BooruPost) -> str:
        """Danbooru 帖缩略图 URL, 优先 720x720 webp. 

        覆盖父类 ``common.SourceClient``. 

        Callers:
            - `routes/booru.py` 搜索结果列表
        """
        return danbooru_preview_url(post)  # type: ignore[arg-type]

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """Danbooru 原始帖补充 ``source`` 和预览 URL. 

        覆盖父类 ``common.SourceClient``. 

        Callers:
            - `sources.py:DanbooruSource.search`
        """
        if post.is_banned or post.is_deleted or not post.file_url:
            return None
        post.source = "danbooru"
        post.preview_url = danbooru_preview_url(post)
        post.sample_url = post.large_file_url or post.file_url
        return post

    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """拉取 Danbooru 帖元数据并写入磁盘. 

        实现 ``common.SourceClient.fetch_meta``. 

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

    async def search(self, tags: str, page: int, limit: int, order: str, extra: str,
                     proxy: str | None, **auth) -> tuple[list[BooruPost], bool]:
        """搜索 Danbooru 帖. 

        实现 ``common.SourceClient.search``. 

        Callers:
            - `common.py:search_posts` / `routes/booru.py:booru_search`
        """
        from services.models import BooruPost

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

        实现 ``common.SourceClient.download``. 

        Callers:
            - ``services/booru.py``
        """
        from services.booru_client.media import _danbooru_fetch_image_bytes as _fetch
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
