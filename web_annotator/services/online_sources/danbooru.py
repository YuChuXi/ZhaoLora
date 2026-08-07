"""Danbooru 来源客户端: 直连官方 JSON API 搜索与下载.

实现 ``base.SourceClient`` 的 ``search`` / ``download`` / ``fetch_meta``，
并封装 Danbooru 专属的网络请求 (TLS 指纹模拟绕过 Cloudflare) 与图片拉取。
API 认证 (login / api_key) 直接从 ``os.environ`` 读取, 不跨层传参。

Callers:
    - ``services.online_sources.__init__`` (注册)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException as CffiError

from services.online_sources.framework.base import SourceClient
from services.online_sources.framework.utils import find_downloaded, pil_can_open, sniff_image_kind

if TYPE_CHECKING:
    from services.models import BooruPost

# Danbooru 需模拟 Chrome TLS 指纹绕过 Cloudflare
_DANBOORU_IMPERSONATE = "chrome"
UNSUPPORTED_FILE_EXT = frozenset({"zip", "mp4", "webm", "swf", "mkv", "avi", "ugoira"})

# (代理, 指纹) -> 复用连接池
_danbooru_clients: dict[tuple[str, str], AsyncSession] = {}


def _danbooru_client() -> AsyncSession:
    """按代理复用的 curl_cffi 连接池, 固定 Chrome TLS 指纹.

    代理从 ``os.environ["UPSTREAM_PROXY"]`` 读取。

    Callers:
        - ``danbooru.py:danbooru_get``
    """
    p = os.environ.get("UPSTREAM_PROXY", "").strip() or None
    key = (p or "", _DANBOORU_IMPERSONATE)
    c = _danbooru_clients.get(key)
    if c is None:
        c = AsyncSession(
            impersonate=_DANBOORU_IMPERSONATE,  # type: ignore[arg-type]
            proxies={"http": p, "https": p} if p else None,  # type: ignore[arg-type]
        )
        _danbooru_clients[key] = c
    return c


def _danbooru_headers(*, accept: str) -> dict[str, str]:
    """Danbooru API 请求头.

    Callers:
        - ``danbooru.py:danbooru_get``
    """
    return {
        "accept": accept, "accept-language": "en-US,en;q=0.9",
        "referer": "https://danbooru.donmai.us/",
        "origin": "https://danbooru.donmai.us",
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def danbooru_get(
    url: str, *, accept: str, timeout: float = 30.0,
    params: dict[str, str | int] | None = None,
    login: str | None = None, api_key: str | None = None,
) -> Any:
    """请求 Danbooru API, 模拟 Chrome TLS 指纹绕过 Cloudflare.

    Args:
        url: 目标 URL。
        accept: Accept 请求头。
        timeout: 超时秒数。
        params: 查询参数。
        login / api_key: Danbooru API 认证, 两者需同时提供。

    Returns:
        curl_cffi 响应对象。

    Callers:
        - ``danbooru.py:DanbooruSource.search`` / ``fetch_meta`` / ``download``
        - ``danbooru.py:_danbooru_fetch_image_bytes``
    """
    p = dict(params) if params else {}
    if login and api_key:
        p["login"] = login
        p["api_key"] = api_key
    return await _danbooru_client().get(
        url, params=p or None, headers=_danbooru_headers(accept=accept),
        timeout=timeout, allow_redirects=True,
    )


def danbooru_preview_url(post) -> str:
    """Danbooru 帖的预览 URL, 优先 720x720 webp, 回退 file_url.

    Callers:
        - ``danbooru.py:DanbooruSource.preview_url`` / ``normalize``
    """
    if post.media_asset:
        variants = post.media_asset.get("variants") or []
        for v in variants:
            if not isinstance(v, dict):
                continue
            t = v.get("type")
            if t == "720x720":
                return str(v.get("url", ""))
        for v in variants:
            if isinstance(v, dict) and v.get("type") in ("360x360", "sample"):
                return str(v.get("url", ""))
    if len(post.md5) >= 4:
        return f"https://cdn.donmai.us/720x720/{post.md5[:2]}/{post.md5[2:4]}/{post.md5}.webp"
    return post.large_file_url or post.preview_url or post.file_url


async def _danbooru_fetch_image_bytes(post) -> bytes:
    """优先原图；失败或不支持时回退 large_file_url。

    Callers:
        - ``danbooru.py:DanbooruSource.download``
    """
    urls: list[str] = [u for u in (post.file_url, post.large_file_url) if u and u not in (None, "")]
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise ValueError("该帖无 file_url(可能需登录或已删除)")

    last_err = "无法下载图片"
    for url in urls:
        resp = await danbooru_get(
            url,
            accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            timeout=60.0,
        )
        if resp.status_code != 200:
            last_err = f"下载 HTTP {resp.status_code}"
            continue
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/html" in ctype or "application/json" in ctype:
            last_err = f"下载到的不是图片(Content-Type: {ctype})"
            continue
        data = resp.content
        kind = sniff_image_kind(data)
        if kind in ("empty", "html", "zip", "video", "webm"):
            last_err = f"不支持该文件类型({kind})"
            continue
        if kind == "unknown" and not pil_can_open(data):
            last_err = f"无法识别的图片数据(大小={len(data)}字节)"
            continue
        return data
    raise ValueError(last_err)


class DanbooruSource(SourceClient):
    name = "danbooru"
    prefix = "d"
    order_tags = {"score": "order:score", "id": "order:id_desc", "id_asc": "order:id"}
    valid_orders = ("score", "id", "id_asc")
    # 每页条数: 上游 posts API 支持的 limit 上限内取固定值
    _PAGE_SIZE = 40

    def preview_url(self, post: BooruPost) -> str:
        """Danbooru 帖缩略图 URL, 优先 720x720 webp.

        覆盖父类 ``base.SourceClient``。

        Callers:
            - `routes/online_sources.py` 搜索结果列表
        """
        return danbooru_preview_url(post)

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """Danbooru 原始帖补充 ``source`` 和预览 URL.

        覆盖父类 ``base.SourceClient``。

        Callers:
            - `danbooru.py:DanbooruSource.search`
        """
        if post.is_banned or post.is_deleted or not post.file_url:
            return None
        post.source = "danbooru"
        post.preview_url = danbooru_preview_url(post)
        post.sample_url = post.large_file_url or post.file_url
        return post

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """拉取 Danbooru 帖元数据并写入磁盘.

        实现 ``base.SourceClient.fetch_meta``。

        Callers:
            - `routes/online_sources.py:_run_refresh`
        """
        from services.models import BooruPost

        meta = await danbooru_get(
            f"https://danbooru.donmai.us/posts/{pid}.json",
            accept="application/json, text/plain, */*", timeout=30.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}")
        self._write_meta(out_dir, pid, BooruPost.model_validate(meta.json()))

    async def search(self, tags: str, page: int, order: str, extra: str) -> tuple[list[BooruPost], bool]:
        """搜索 Danbooru 帖.

        每页条数由本来源固定 (``_PAGE_SIZE``), 不随调用方变化。实现
        ``base.SourceClient.search``。

        Callers:
            - ``base.py:search_posts`` / ``routes/online_sources.py:search``
        """
        from services.models import BooruPost

        query = self.build_search_query(tags, order=order, extra=extra)
        resp = await danbooru_get(
            "https://danbooru.donmai.us/posts.json",
            accept="application/json, text/plain, */*", timeout=30.0,
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

    async def download(self, pid: str, out_dir: Path, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False) -> dict:
        """下载 Danbooru 原图并写旁路标签.

        实现 ``base.SourceClient.download``。

        Callers:
            - ``base.py:SourceClient.download``
        """
        from services.models import BooruPost

        stem = self.file_stem(pid)
        image_path = out_dir / f"{stem}.png"
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, pid, file_stem_fn=self.file_stem)

        meta = await danbooru_get(
            f"https://danbooru.donmai.us/posts/{pid}.json",
            accept="application/json, text/plain, */*", timeout=60.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}")
        api_post = BooruPost.model_validate(meta.json())
        if api_post.file_ext in UNSUPPORTED_FILE_EXT and not api_post.large_file_url:
            raise ValueError(f"不支持的类型: .{api_post.file_ext}")

        if not overwrite and existing is not None and tags_path.is_file():
            self._write_meta(out_dir, pid, api_post)
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid, "tags_path": str(tags_path)}

        data = await _danbooru_fetch_image_bytes(api_post)
        self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        caption = self._build_caption(api_post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(out_dir, pid, api_post)
        return {"path": str(image_path), "stem": stem, "id": pid, "tags_path": str(tags_path)}