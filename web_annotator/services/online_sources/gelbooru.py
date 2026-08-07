"""Gelbooru 来源客户端: 直连官方 JSON API 搜索与下载.

实现 ``base.SourceClient`` 的 ``search`` / ``download`` / ``fetch_meta``,
并封装 Gelbooru 专属的 API 客户端 (``GelbooruClient``) 与图片代理
(``fetch_gelbooru_bytes``)。

Callers:
    - ``services.online_sources.__init__`` (注册)
    - ``routes.online_sources`` (图片代理)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.online_sources.framework import http
from services.online_sources.framework.base import SourceClient
from services.online_sources.framework.utils import find_downloaded

if TYPE_CHECKING:
    from services.models import BooruPost

_GELBOORU_BASE = "https://gelbooru.com/index.php"


class GelbooruClient:
    """Gelbooru JSON API 客户端.

    Callers:
        - ``gelbooru.py:GelbooruSource``
    """

    def __init__(self, proxy: str | None = None) -> None:
        """初始化客户端.

        Args:
            proxy: 上游代理地址, None 表示直连.
        """
        self._proxy = proxy

    async def _api(self, params: dict[str, str | int]) -> dict:
        """调用 Gelbooru JSON API, 返回解析后的响应.

        Args:
            params: 查询参数 (含 page=dapi&s=post&q=index&json=1 等).

        Returns:
            dict: API 响应 JSON.

        Raises:
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruClient.get_post`` / ``search`` / ``get_tag_types``
        """
        resp = await http.request(_GELBOORU_BASE, params=params, proxy=self._proxy)
        resp.raise_for_status()
        return resp.json()

    async def get_post(self, pid: int) -> dict:
        """获取单帖元数据.

        Args:
            pid: 帖子 ID.

        Returns:
            dict: 帖子数据; 未找到时返回空字典.

        Raises:
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruSource.download`` / ``fetch_meta``
            - ``GelbooruClient.get_image``
        """
        data = await self._api({
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "id": str(pid),
        })
        post = data.get("post")
        if isinstance(post, list):
            return post[0] if post else {}
        return post or {}

    async def search(self, tags: str, pid: int) -> dict:
        """搜索帖子.

        Args:
            tags: 标签查询串.
            pid: 页码 (0-based).

        Returns:
            dict: API 响应 JSON.

        Raises:
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruSource.search``
        """
        return await self._api({
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "tags": tags, "pid": str(pid), "limit": "100",
        })

    async def get_image(self, pid: int, path: str) -> None:
        """下载帖子原图到本地文件.

        Args:
            pid: 帖子 ID.
            path: 目标文件路径.

        Raises:
            ValueError: 帖子无 file_url.
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruSource.download``
        """
        post = await self.get_post(pid)
        file_url = post.get("file_url")
        if not file_url:
            raise ValueError(f"帖子 {pid} 无 file_url")
        data, _ = await http.get_bytes(
            file_url, referer="https://gelbooru.com/", proxy=self._proxy,
        )
        Path(path).write_bytes(data)

    async def get_tag_types(self, tags: list[str]) -> dict[str, int]:
        """批量查询标签类型.

        Args:
            tags: 标签名列表.

        Returns:
            dict[str, int]: 标签名 -> 类型编号 (0=general, 1=artist, 3=copyright, 4=character, 5=meta).

        Raises:
            http.NetworkError: 网络错误.

        Callers:
            - ``GelbooruSource.download``
        """
        data = await self._api({
            "page": "dapi", "s": "tag", "q": "index", "json": "1",
            "names": " ".join(tags),
        })
        result: dict[str, int] = {}
        for tag in data.get("tag", []):
            if isinstance(tag, dict):
                name = tag.get("name")
                ttype = tag.get("type")
                if name and ttype is not None:
                    result[name] = int(ttype)
        return result


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
            - `base.SourceClient:build_search_query`
        """
        tokens = [x for x in extra.split() if not x.lower().startswith("date:")]
        return " ".join(tokens)

    def is_media_url(self, url: str) -> bool:
        """仅允许 gelbooru.com 图床.

        覆盖父类 ``base.SourceClient``.

        Callers:
            - `routes/online_sources.py` 缩略图代理白名单检查
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

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """拉取 Gelbooru 帖元数据并写入磁盘.

        实现 ``base.SourceClient.fetch_meta``.

        Callers:
            - `routes/online_sources.py:_run_refresh`
        """
        api = GelbooruClient()
        raw = await api.get_post(int(pid))
        self._write_meta(out_dir, pid, BooruPost.model_validate(raw))

    async def search(self, tags: str, page: int, order: str, extra: str) -> tuple[list[BooruPost], bool]:
        """搜索 Gelbooru 帖.

        每页 100 条由上游固定, 页大小不由调用方决定。实现
        ``base.SourceClient.search``。

        Callers:
            - `base.py:search_posts` / `routes/online_sources.py:search`
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

    async def download(self, pid: str, out_dir: Path, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> dict:
        """下载 Gelbooru 原图并写旁路标签.

        实现 ``base.SourceClient.download``.

        Callers:
            - ``base.py:SourceClient.download`` (经 ``media.download_post`` 委托)
        """
        from services.online_sources.framework.utils import convert_path_to_png
        from services.models import BooruPost

        api = GelbooruClient()
        stem = self.file_stem(pid)
        image_path = out_dir / f"{stem}.png"
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, pid, file_stem_fn=self.file_stem)

        if not overwrite and existing is not None and tags_path.is_file():
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid, "tags_path": str(tags_path)}

        raw = await api.get_post(int(pid))
        api_post = BooruPost.model_validate(raw)
        if not api_post.file_url:
            raise ValueError("无 file_url")
        if overwrite or not image_path.is_file():
            tmp_ext = http.ext_from_url(api_post.file_url, ".bin")
            tmp_path = out_dir / f"{stem}.tmp{tmp_ext}"
            await api.get_image(int(pid), str(tmp_path))
            convert_path_to_png(tmp_path, image_path)
            self._clean_old_files(out_dir, pid, image_path)

        if anima_format:
            raw = await _categorize_gelbooru_tags(raw, api)
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
        - ``GelbooruSource.download``
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