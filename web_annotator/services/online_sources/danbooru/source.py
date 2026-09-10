"""Danbooru 来源适配器: 实现 ``SourceClient`` 的搜索 / 下载 / 元数据.

API 认证 (login / api_key) 直接从 ``os.environ`` 读取, 不跨层传参。

Callers:
    - ``services/online_sources/danbooru/__init__.py:`` (导出)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from services.core.atomic import atomic_write_text
from services.online_sources.danbooru import client as _client
from services.online_sources.framework.base import ProgressFn, SourceClient, build_caption
from services.online_sources.framework.media import download_raw_file, post_is_video
from services.online_sources.framework.utils import find_downloaded, pil_can_open, sniff_image_kind

if TYPE_CHECKING:
    from services.models import BooruPost

UNSUPPORTED_FILE_EXT = frozenset({"zip", "mp4", "webm", "swf", "mkv", "avi", "ugoira"})


def _to_model_data(raw: dict) -> dict:
    """将 Danbooru API 原始响应映射为 ``BooruPost`` 统一字段名.

    Args:
        raw: Danbooru API 原始帖子字典 (含大量额外字段, 原样保留进 meta;
            ``id`` 与 ``created_at`` 为 API 必有字段, 后者契约即 ISO8601).

    Returns:
        dict: 补齐 width/height/tags/preview_url 统一字段、id 转文本、
            ``created_at`` 消费为 ``posted_at`` 后的数据.

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource.fetch_meta``
        - ``services/online_sources/danbooru/source.py:DanbooruSource.search``
    """
    out = {**raw}
    out.setdefault("width", out.get("image_width"))
    out.setdefault("height", out.get("image_height"))
    out.setdefault("tags", out.get("tag_string"))
    out.setdefault("preview_url", out.get("preview_file_url"))
    out["id"] = str(out["id"])
    out["posted_at"] = out.pop("created_at")
    return out


def preview_url(post) -> str:
    """Danbooru 帖的预览 URL, 优先 720x720 webp, 回退 file_url.

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource.preview_url``
        - ``services/online_sources/danbooru/source.py:normalize``
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


async def _fetch_image_bytes(post) -> bytes:
    """优先原图；失败或不支持时回退 large_file_url。

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource.download``
    """
    urls: list[str] = [u for u in (post.file_url, post.large_file_url) if u and u not in (None, "")]
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise ValueError("该帖无 file_url(可能需登录或已删除)")

    last_err = "无法下载图片"
    for url in urls:
        resp = await _client.get(
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
    """Danbooru 图站来源客户端 (页码式).

    Callers:
        - ``services/online_sources/framework/base.py:register_source``
    """

    name = "danbooru"
    prefix = "d"
    order_tags = {"score": "order:score", "id": "order:id_desc", "id_asc": "order:id"}
    valid_orders = ("score", "id", "id_asc")

    @staticmethod
    def normalize(post: BooruPost) -> BooruPost | None:
        """Danbooru 原始帖补充 ``source`` 和预览 URL.

        覆盖父类 ``base.SourceClient``。

        Callers:
            - ``services/online_sources/danbooru/source.py:DanbooruSource.search``
        """
        if post.is_banned or post.is_deleted or not post.file_url:
            return None
        post.source = "danbooru"
        post.preview_url = preview_url(post)
        post.sample_url = post.large_file_url or post.file_url
        post.is_video = post_is_video(post)
        return post

    def split(self, stem: str) -> str:
        """从 stem 拆出本来源 pid, 非本来源格式抛 ValueError.

        pid 为前缀后纯数字文本.

        Callers:
            - ``services/online_sources/framework/base.py:detect_source``
        """
        rest = stem[len(self.prefix):] if stem.startswith(self.prefix) else ""
        if not rest.isdigit():
            raise ValueError(f"非法 stem: {stem}")
        return rest

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """拉取 Danbooru 帖元数据并写入磁盘.

        实现 ``base.SourceClient.fetch_meta``。

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
        from services.models import BooruPost

        meta = await _client.get(
            f"https://danbooru.donmai.us/posts/{pid}.json",
            accept="application/json, text/plain, */*", timeout=30.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}")
        self._write_meta(out_dir, pid, BooruPost.model_validate(_to_model_data(meta.json())))

    async def autocomplete(self, query: str, limit: int = 10) -> list[dict]:
        """标签自动补全.

        实现 ``base.SourceClient.autocomplete``。

        Callers:
            - ``routes/online_sources.py:tag_suggest``
        """
        return await _client.autocomplete(query, limit)

    async def search(self, tags: str, page: int, order: str, extra: str) -> tuple[list[BooruPost], bool]:
        """搜索 Danbooru 帖.

        ``base.SourceClient.search``。

        Callers:
            - ``services/online_sources/framework/base.py:search_posts``
            - ``routes/online_sources.py:search``
        """
        from services.models import BooruPost

        query = self.build_search_query(tags, order=order, extra=extra)
        resp = await _client.get(
            "https://danbooru.donmai.us/posts.json",
            accept="application/json, text/plain, */*", timeout=30.0,
            params={"tags": query, "page": max(1, page)},
            login=os.environ.get("DANBOORU_LOGIN", "").strip() or None,
            api_key=os.environ.get("DANBOORU_API_KEY", "").strip() or None,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {resp.status_code}")
        data = resp.json()
        if not isinstance(data, list):
            return [], False
        has_more = len(data) > 0
        out: list[BooruPost] = []
        for raw_post in data:
            if not isinstance(raw_post, dict):
                continue
            n = self.normalize(BooruPost.model_validate(_to_model_data(raw_post)))
            if n is not None:
                out.append(n)
        return out, has_more

    async def download(self, post: BooruPost, out_dir: Path, *,
                       overwrite: bool = False, anima_format: bool = True,
                       include_meta_tags: bool = False, keep_underscores: bool = False,
                       emit: ProgressFn | None = None) -> dict:
        """下载 Danbooru 原图并写旁路标签.

        直接使用 search 阶段已获取的完整 ``post`` (含原图 URL 与标签字段),
        帖文元数据一律以入参为准。视频帖 (``post.is_video``)
        经由 ``download_raw_file`` 下载原始文件。实现 ``base.SourceClient.download``。

        Args:
            emit: 下载进度回调, 图片来源忽略 (透传签名统一).

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.download``
        """
        pid = post.id
        stem = Path(post.filename).stem
        image_path = out_dir / post.filename
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, post.filename)

        if post.is_video:
            return await download_raw_file(post, out_dir)

        if post.file_ext in UNSUPPORTED_FILE_EXT and not post.large_file_url:
            raise ValueError(f"不支持的类型: .{post.file_ext}")

        if not overwrite and existing is not None and tags_path.is_file():
            return {"skipped": True, "path": str(existing), "stem": stem, "id": pid, "tags_path": str(tags_path)}

        data = await _fetch_image_bytes(post)
        await self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        caption = build_caption(post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        atomic_write_text(tags_path, caption + "\n")
        self._write_meta(out_dir, pid, post)
        return {"path": str(image_path), "stem": stem, "id": pid, "tags_path": str(tags_path)}
