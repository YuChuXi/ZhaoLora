"""Gelbooru 来源适配器: 实现 ``SourceClient`` 的搜索 / 下载 / 元数据.

Callers:
    - ``services/online_sources/gelbooru/__init__.py:`` (导出)
"""

from __future__ import annotations

from pathlib import Path

from services.online_sources.framework.base import ProgressFn, SourceClient
from services.online_sources.framework.media import download_raw_file, post_is_video
from services.online_sources.framework.utils import find_downloaded
from services.online_sources.gelbooru.client import GelbooruClient
from services.models import BooruPost


class GelbooruSource(SourceClient):
    name = "gelbooru"
    prefix = "g"
    order_tags = {"score": "sort:score:desc", "id": "sort:id:desc", "id_asc": "sort:id:asc"}
    valid_orders = ("score", "id", "id_asc")
    image_referer = "https://gelbooru.com/"
    # 缩略图更快, 大图放宽超时
    _IMAGE_TIMEOUTS = {"thumb": 30.0, "sample": 45.0}

    def image_timeout_for(self, kind: str) -> float:
        return self._IMAGE_TIMEOUTS.get(kind, self.image_timeout)

    def filter_extra(self, extra: str) -> str:
        """Gelbooru 忽略 date: 元标签, 并合并多评级为 OR.

        Gelbooru 的多评级用 ``(rating:a) OR (rating:b)`` 逐项括号大写
        连接(与 danbooru 的小写 ``or`` 连写不同)。覆盖父类
        ``base.SourceClient``。

        Callers:
            - ``services/online_sources/framework/base.py:build_search_query``
        """
        tokens = [x for x in extra.split() if not x.lower().startswith("date:")]
        return " ".join(tokens)

    def is_media_url(self, url: str) -> bool:
        """仅允许 gelbooru.com 图床.

        覆盖父类 ``base.SourceClient``.

        Callers:
            - ``routes/online_sources.py:`` 缩略图代理白名单检查
        """
        return "gelbooru.com" in url

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """Gelbooru 原始帖补充 ``source`` 和回退 URL.

        覆盖父类 ``base.SourceClient``.

        Callers:
            - ``services/online_sources/gelbooru/source.py:GelbooruSource.search``
        """
        if not post.file_url:
            return None
        post.source = "gelbooru"
        post.preview_url = post.preview_url or post.sample_url or post.file_url
        post.sample_url = post.sample_url or post.preview_url
        post.is_video = post_is_video(post)
        return post

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """拉取 Gelbooru 帖元数据并写入磁盘.

        实现 ``base.SourceClient.fetch_meta``.

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
        api = GelbooruClient()
        raw = await api.get_post(int(pid))
        self._write_meta(out_dir, pid, BooruPost.model_validate(raw))

    async def autocomplete(self, query: str, limit: int = 10) -> list[dict]:
        """标签自动补全.

        实现 ``base.SourceClient.autocomplete``。

        Callers:
            - ``routes/online_sources.py:tag_suggest``
        """
        api = GelbooruClient()
        return await api.autocomplete(query, limit)

    async def search(self, tags: str, page: int, order: str, extra: str) -> tuple[list[BooruPost], bool]:
        """搜索 Gelbooru 帖.

        每页 100 条由上游固定, 页大小不由调用方决定。实现
        ``base.SourceClient.search``。

        Callers:
            - ``services/online_sources/framework/base.py:search_posts`` / `routes/online_sources.py:search`
        """
        from services.models import BooruPost

        api = GelbooruClient()
        query = self.build_search_query(tags, order=order, extra=extra)
        pid = max(0, page - 1)
        raw_resp = await api.search(query, pid)
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

    async def download(self, post: BooruPost, out_dir: Path, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False,
                       emit: ProgressFn | None = None) -> dict:
        """下载 Gelbooru 原图并写旁路标签.

        直接使用 search 阶段已持有的 ``post.file_url`` 与 ``post.tags`` 写入,
        不再重新请求 ``get_post``; 仅 anima 格式需要额外查询标签类型。
        视频帖 (``post.is_video``) 经由 ``download_raw_file`` 下载原始文件。
        实现 ``base.SourceClient.download``。

        Args:
            emit: 下载进度回调, 图片来源忽略 (透传签名统一).

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.download`` (经 ``media.download_post`` 委托)
        """
        api = GelbooruClient()
        pid = post.id
        stem = Path(post.filename).stem
        image_path = out_dir / post.filename
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, post.filename)

        if post.is_video:
            return await download_raw_file(post, out_dir)

        if not overwrite and existing is not None and tags_path.is_file():
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid, "tags_path": str(tags_path)}

        if not post.file_url:
            raise ValueError("无 file_url")

        if overwrite or not image_path.is_file():
            await api.get_image(post.file_url, str(image_path), referer=self.image_referer)
            self._clean_old_files(out_dir, pid, image_path)

        api_post = post
        if anima_format:
            raw = await _categorize_gelbooru_tags(api_post.model_dump(), api)
            api_post = BooruPost.model_validate(raw)
        caption = self._build_caption(api_post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(out_dir, pid, api_post)
        return {"path": str(image_path), "stem": stem, "id": pid, "tags_path": str(tags_path)}


# 标签类型缓存, 进程内复用
_tag_types_cache: dict[str, int] = {}


async def _categorize_gelbooru_tags(post: dict, api: GelbooruClient) -> dict:
    """查询 Gelbooru 标签类型并写入 Danbooru 兼容字段, 结果缓存在内存.

    Args:
        post: Gelbooru API 返回的帖子数据.
        api: GelbooruClient 实例.

    Returns:
        含 tag_string_* 字段的新 dict.

    Callers:
        - ``services/online_sources/gelbooru/source.py:GelbooruSource.download``
    """
    all_names = str(post.get("tags") or "").split()
    if not all_names:
        return dict(post)
    missing = [n for n in all_names if n not in _tag_types_cache]
    if missing:
        types = await api.get_tag_types(missing)
        _tag_types_cache.update(types)
    cats: dict[int, list[str]] = {0: [], 1: [], 3: [], 4: [], 5: []}
    for t in all_names:
        cats[_tag_types_cache.get(t, 0)].append(t)
    post = dict(post)
    post["tag_string_general"] = " ".join(cats[0])
    post["tag_string_artist"] = " ".join(cats[1])
    post["tag_string_copyright"] = " ".join(cats[3])
    post["tag_string_character"] = " ".join(cats[4])
    post["tag_string_meta"] = " ".join(cats[5])
    return post
