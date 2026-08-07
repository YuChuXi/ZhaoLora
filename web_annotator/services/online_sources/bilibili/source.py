"""B 站来源客户端: 将用户动态的每张图映射为一张 BooruPost 卡片.

`tags` 参数承载 B 站 UID, `extra` 为筛选串 (``type:`` / ``after:`` /
``before:`` / ``keyword:``)。搜索时先分页拉取该用户全部动态并解析, 按
筛选条件过滤后把所有可展示图片展开为扁平列表, 缓存于进程内并按其序号
分页返回; 下载时按 ``{动态ID}_{图片序号}`` 的 pid 解析原图 URL 并落盘。

Callers:
    - ``services.online_sources.__init__`` (注册)
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any

from services.online_sources.framework.base import SourceClient
from services.online_sources.bilibili.client import fetch_bili_bytes, fetch_dynamics, resolve_image
from services.online_sources.bilibili.models import DynamicRecord
from services.online_sources.framework.utils import find_downloaded
from services.models import BooruPost

# 游标分页状态: 键为 (uid, type, after, before, keyword),
# 值为 {offsets: 各页请求游标, pages: 各页展开的帖子, has_more: 上游是否还有}
_listing_state: dict[tuple[Any, ...], dict[str, Any]] = {}
_listing_locks: dict[tuple[Any, ...], asyncio.Lock] = {}
_state_guard = asyncio.Lock()

# 动态类型 -> 筛选页展示语义
_TYPE_MAP = {
    "DYNAMIC_TYPE_DRAW": "image",
    "DYNAMIC_TYPE_OPUS": "image",
    "DYNAMIC_TYPE_AV": "video",
    "DYNAMIC_TYPE_FORWARD": "forward",
}


class BilibiliSource(SourceClient):
    """从 B 站用户动态拉取图片的来源客户端.

    实现 ``base.SourceClient`` 的 ``search`` / ``download``。

    Callers:
        - ``__init__.py:register_source``
    """

    name = "bilibili"
    prefix = "bi"
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()
    image_hosts = ("hdslb.com",)
    image_referer = "https://www.bilibili.com/"

    def parse_query(self, tags: str, extra: str) -> dict[str, str]:
        """解析 B 站查询串: ``tags`` 为 UID, ``extra`` 为 ``type:``/``after:``/``before:``/``keyword:``.

        类型可多选, 以多个 ``type:`` token 表达, 结果以逗号分隔去重保序
        (字符串, 可直接用作分页缓存键)。

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
        types: list[str] = []
        for token in (extra or "").split():
            for key in ("type", "after", "before", "keyword"):
                if token.startswith(key + ":"):
                    val = token[len(key) + 1:].strip()
                    if key == "type":
                        if val:
                            types.append(val)
                    else:
                        out[key] = val
                    break
        out["type"] = ",".join(dict.fromkeys(types))
        return out

    def _matches(self, record: DynamicRecord, f: dict[str, str]) -> bool:
        """判断动态是否满足类型 / 关键词 / 时间筛选.

        Callers:
            - `BilibiliSource.search`
        """
        ttype = _TYPE_MAP.get(record.type or "", "other")
        if f["type"] and ttype not in f["type"].split(","):
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

    def _expand(self, record: DynamicRecord) -> list[BooruPost]:
        """将一条动态展开为 BooruPost 列表.

        转发动态取原文的图; 视频动态取封面; 无可用图片时返回空列表。
        帖子 id 前缀用外层 ``record.id``, 避免原动态与转发动态并存时
        展开出相同 id。缩略图 / 样图 URL 由原图 URL 加 B 站 CDN 后缀派生。
        B 站视频封面为平台规范尺寸 1920x1080 (实测确认), 直接作为真实宽高,
        不另做探测请求。

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
            items = [(source.video.cover, 1920, 1080)]
        else:
            return []
        return [
            BooruPost(
                source=self.name,
                id=f"{record.id}_{i}",
                width=w,
                height=h,
                file_url=url,
                preview_url=url + "@480w_480h_1e.webp",
                sample_url=url + "@1280w_1280h_1e.webp",
                tags=text,
            )
            for i, (url, w, h) in enumerate(items)
        ]

    async def search(
        self,
        tags: str,
        page: int,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool]:
        """按 UID 与筛选条件搜索动态图片, 返回 (posts, has_more).

        B 站 feed 是游标分页, 页大小由接口决定 (约 20 条动态)。按需拉取:
        ``page=N`` 时若上游页尚未拉够, 才从游标继续拉取, 返回第 N 页动态
        展开的图片; 已拉取的页缓存复用。

        Callers:
            - `base.py:search_posts` / `routes/online_sources.py:search`
        """
        f = self.parse_query(tags, extra)
        uid = int(f["uid"])
        key = (uid, f["type"], f["after"], f["before"], f["keyword"])

        async with _state_guard:
            st = _listing_state.get(key)
            if st is None:
                st = {"offsets": [""], "pages": [], "has_more": True}
                _listing_state[key] = st
            lock = _listing_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                _listing_locks[key] = lock

        async with lock:
            while len(st["pages"]) < page and st["has_more"]:
                records, next_offset, has_more = await fetch_dynamics(
                    uid, offset=st["offsets"][-1]
                )
                page_posts = [
                    post
                    for record in records
                    if self._matches(record, f)
                    for post in self._expand(record)
                ]
                st["pages"].append(page_posts)
                st["offsets"].append(next_offset)
                st["has_more"] = has_more

        idx = min(page - 1, len(st["pages"]) - 1)
        slice_posts = st["pages"][idx] if idx >= 0 else []
        has_more = idx + 1 < len(st["pages"]) or st["has_more"]

        return slice_posts, has_more

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

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """按 pid 重新解析并写入帖元数据.

        实现 ``base.SourceClient.fetch_meta``。当前 ``detect_source`` 不会
        匹配 bilibili 文件名 (pid 含 `_`), 故仅在手动调用时生效。

        Callers:
            - `routes/online_sources.py:_run_refresh`
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
        post: BooruPost,
        out_dir: Path,
        *,
        overwrite: bool = False,
        anima_format: bool = True,
        include_meta_tags: bool = False,
        keep_underscores: bool = False,
    ) -> dict:
        """下载动态某张原图为 PNG, 并写旁路标签 (正文) 与 meta.

        直接使用 search 阶段已持有的 ``post.file_url`` 与 ``post.tags`` 落盘,
        不再重新请求动态元数据。实现 ``base.SourceClient.download``。

        Callers:
            - `base.py:SourceClient.download` (经 ``media.download_post`` 委托)
        """
        pid = post.id
        stem = Path(post.filename).stem
        image_path = out_dir / post.filename
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, post.filename)

        if not overwrite and existing is not None and tags_path.is_file():
            return {
                "skipped": True,
                "path": str(existing),
                "stem": stem,
                "id": pid,
                "tags_path": str(tags_path),
            }

        url = (post.file_url or "").strip()
        if not url:
            raise ValueError(f"pid {pid} 无媒体 URL")
        caption = (post.tags or "").strip()
        if include_meta_tags:
            caption = (caption + (" " + self.name) if caption else self.name).strip()

        data, _ = await fetch_bili_bytes(url)
        await self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
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