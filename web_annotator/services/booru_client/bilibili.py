"""B 站来源客户端: 将用户动态的每张图映射为一张 BooruPost 卡片.

`tags` 参数承载 B 站 UID, `extra` 为筛选串 (``type:`` / ``after:`` /
``before:`` / ``keyword:``)。搜索时先分页拉取该用户全部动态并解析, 按
筛选条件过滤后把所有可展示图片展开为扁平列表, 缓存于进程内并按其序号
分页返回; 下载时按 ``{动态ID}_{图片序号}`` 的 pid 解析原图 URL 并落盘。

Callers:
    - ``services.booru_client.__init__`` (注册)
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from services.bilibili.client import fetch_bili_bytes, fetch_dynamics, resolve_image
from services.bilibili.models import DynamicRecord
from services.booru_client.common import SourceClient
from services.booru_client.utils import find_downloaded
from services.models import BooruPost

# 图片卡片的进程内缓存: 键为 (uid, type, after, before, keyword), 值为帖子列表
_listing_cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
_listing_lock = threading.Lock()

# 动态类型 -> 筛选页展示语义
_TYPE_MAP = {
    "DYNAMIC_TYPE_DRAW": "image",
    "DYNAMIC_TYPE_OPUS": "image",
    "DYNAMIC_TYPE_AV": "video",
    "DYNAMIC_TYPE_FORWARD": "forward",
}


class BilibiliSource(SourceClient):
    """从 B 站用户动态拉取图片的来源客户端.

    实现 ``common.SourceClient`` 的 ``search`` / ``download``。

    Callers:
        - ``__init__.py:register_source``
    """

    name = "bilibili"
    prefix = "bi"
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()

    def parse_query(self, tags: str, extra: str) -> dict[str, str]:
        """解析 B 站查询串: ``tags`` 为 UID, ``extra`` 为 ``type:``/``after:``/``before:``/``keyword:``.

        Callers:
            - `BilibiliSource.search`
        """
        out = {
            "uid": str(tags).strip(),
            "type": "",
            "after": "",
            "before": "",
            "keyword": "",
        }
        for token in (extra or "").split():
            for key in ("type", "after", "before", "keyword"):
                if token.startswith(key + ":"):
                    out[key] = token[len(key) + 1:].strip()
                    break
        return out

    def _matches(self, record: DynamicRecord, f: dict[str, str]) -> bool:
        """判断动态是否满足类型 / 关键词 / 时间筛选.

        Callers:
            - `BilibiliSource.search`
        """
        ttype = _TYPE_MAP.get(record.type or "", "other")
        if f["type"] and ttype != f["type"]:
            return False
        if f["keyword"] and f["keyword"].lower() not in record.text.lower():
            return False
        if (f["after"] or f["before"]):
            if not record.pub_ts:
                return False
            ts = int(record.pub_ts)
            d = datetime.fromtimestamp(ts).date()
            if f["after"] and d < date.fromisoformat(f["after"]):
                return False
            if f["before"] and d > date.fromisoformat(f["before"]):
                return False
        return True

    def _expand(self, record: DynamicRecord) -> list[dict[str, Any]]:
        """将一条动态展开为图片帖子列表.

        转发动态取原文的图; 视频动态取封面; 无可用图片时返回空列表。

        Callers:
            - `BilibiliSource.search`
        """
        source = record.forwarded if record.forwarded is not None else record
        text = source.text or record.text
        if source.images:
            items = [
                (img.url, img.width or 0, img.height or 0) for img in source.images
            ]
        elif source.video and source.video.cover:
            items = [(source.video.cover, 0, 0)]
        else:
            return []
        return [
            {"id": f"{source.id}_{i}", "url": url, "width": w, "height": h, "text": text}
            for i, (url, w, h) in enumerate(items)
        ]

    async def search(
        self,
        tags: str,
        page: int,
        limit: int,
        order: str,
        extra: str,
        proxy: str | None,
        **auth,
    ) -> tuple[list[BooruPost], bool]:
        """按 UID 与筛选条件搜索动态图片, 返回 (posts, has_more).

        首次请求 (page=1) 拉取该用户全部动态并构建图片帖子列表, 缓存后按
        页码切片返回; 后续页码直接读缓存。

        Callers:
            - `common.py:search_posts` / `routes/booru.py:booru_search`
        """
        f = self.parse_query(tags, extra)
        uid = int(f["uid"])
        key = (uid, f["type"], f["after"], f["before"], f["keyword"])

        with _listing_lock:
            posts = _listing_cache.get(key)
        if posts is None:
            records = await fetch_dynamics(uid)
            posts = [
                post
                for record in records
                if self._matches(record, f)
                for post in self._expand(record)
            ]
            with _listing_lock:
                _listing_cache[key] = posts

        start = (page - 1) * limit
        end = start + limit
        slice_posts = posts[start:end]
        has_more = end < len(posts)

        out: list[BooruPost] = []
        for p in slice_posts:
            out.append(
                BooruPost(
                    source=self.name,
                    id=p["id"],
                    width=p["width"],
                    height=p["height"],
                    file_url=p["url"],
                    preview_url=p["url"],
                    sample_url=p["url"],
                    tags=p["text"],
                )
            )
        return out, has_more

    def _split_pid(self, pid: str) -> tuple[str, int]:
        """将 ``{动态ID}_{图片序号}`` 拆为 (动态ID, 序号).

        Callers:
            - `BilibiliSource.download`
            - `BilibiliSource.fetch_meta`
        """
        idx = pid.rfind("_")
        if idx <= 0:
            raise ValueError(f"非法 id: {pid}")
        return pid[:idx], int(pid[idx + 1:])

    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """按 pid 重新解析并写入帖元数据.

        实现 ``common.SourceClient.fetch_meta``。当前 ``detect_source`` 不会
        匹配 bilibili 文件名 (pid 含 `_`), 故仅在手动调用时生效。

        Callers:
            - `routes/booru.py:_run_refresh`
        """
        dynamic_id, index = self._split_pid(pid)
        url, text = await resolve_image(dynamic_id, index)
        self._write_meta(
            out_dir,
            pid,
            BooruPost(source=self.name, id=pid, file_url=url, tags=text),
        )

    async def download(
        self,
        pid: str,
        out_dir: Path,
        proxy: str | None,
        *,
        overwrite: bool = False,
        anima_format: bool = True,
        include_meta_tags: bool = False,
        keep_underscores: bool = False,
    ) -> dict:
        """下载动态某张原图为 PNG, 并写旁路标签 (正文) 与 meta.

        Callers:
            - `common.py:SourceClient.download` (经 ``media.download_post`` 委托)
        """
        dynamic_id, index = self._split_pid(pid)
        url, text = await resolve_image(dynamic_id, index)

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

        data, _ = await fetch_bili_bytes(url)
        self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        caption = re.sub(r"\s+", " ", (text or "").strip()).strip()
        if include_meta_tags:
            caption = (caption + (" " + self.name) if caption else self.name).strip()
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(
            out_dir,
            pid,
            BooruPost(source=self.name, id=pid, file_url=url, tags=caption),
        )
        return {
            "path": str(image_path),
            "stem": stem,
            "id": pid,
            "tags_path": str(tags_path),
        }