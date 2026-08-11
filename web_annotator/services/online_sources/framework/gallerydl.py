"""gallery-dl 驱动的来源客户端基类.

把 gallery-dl 共用的脚手架 (元数据拉取、增量分页缓存、图片直链下载、
旁路标签与 meta 写入) 统一到 ``GalleryDLSource``。来源子类只需实现
特有的查询键、URL 构造、元数据换算与认证配置, 即可获得完整的
``search`` / ``download`` / ``fetch_meta`` 能力。

Callers:
    - ``services.online_sources.x.source``
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gallery_dl import config
from gallery_dl.extractor import find as gallery_find
from gallery_dl.extractor.message import Message

from services.online_sources.framework.http import HttpClient
from services.online_sources.framework.base import ProgressFn, SourceClient
from services.online_sources.framework.session import SessionStore
from services.online_sources.framework.utils import find_downloaded
from services.models import BooruPost


# 会话游标的"未开始"哨兵 (区别于已拉取完毕的 None)
_NOT_STARTED: Any = object()


@dataclass
class _Cache:
    """某查询已拉取的图片卡片与续拉游标.

    Attributes:
        posts: 已展开的图片卡片列表 (按拉取序排列).
        cursor: 上次拉取结束时的续拉游标; None 表示已拉取完毕或来源不支持续拉.
    """
    posts: list[BooruPost]
    cursor: str | None


class GalleryDLSource(SourceClient):
    """gallery-dl 驱动的来源客户端基类.

    子类需实现以下钩子:

    - ``_search_key(tags, extra)``: 把查询参数归一为可哈希缓存键, 空查询返回 None。
    - ``_search_url(key)``: 由缓存键构造 gallery-dl 可解析的搜索页 URL。
    - ``_detail_url(parts)``: 由 ``_split_pid`` 拆解出的元组构造单条媒体页 URL。
    - ``_split_pid(pid)``: 把卡片 pid 拆为下载定位所需的结构化元组。
    - ``_post_from_item(url, kwdict, key)``: 把单条媒体元数据换算为 BooruPost。
    - ``_caption_from_item(kwdict)``: 从媒体元数据提取旁路标签文本。
    - ``_auth_overrides()``: 认证相关的临时配置项(默认空)。

    子类可覆盖 ``_INCR_BATCH``(单批增量条数, 亦即每页卡片数;
    None 表示一次性全量拉取并以单页返回全部)。
    """

    # 子类可覆盖
    _INCR_BATCH: int | None = None
    session_based = True
    writes_caption = False

    def __init__(self) -> None:
        """初始化来源级的会话存储。"""
        self._sessions = SessionStore()

    # ---- 子类必须实现的钩子 ----

    def _search_key(self, tags: str, extra: str) -> Any | None:
        """把查询参数归一为可哈希的缓存键.

        Args:
            tags: 查询词(画师 ID / 用户名 / 标签等).
            extra: 附加筛选串.

        Returns:
            Any | None: 可哈希缓存键; 无有效查询时返回 None.

        Callers:
            - `GalleryDLSource.search`
        """
        raise NotImplementedError

    def _search_url(self, key: Any) -> str:
        """由缓存键构造 gallery-dl 可解析的搜索页 URL.

        Args:
            key: ``_search_key`` 返回的缓存键.

        Returns:
            str: 搜索页 URL.

        Callers:
            - `GalleryDLSource._ensure_cached`
        """
        raise NotImplementedError

    def _detail_url(self, parts: tuple) -> str:
        """由拆分后的 pid 构造单条媒体(作品/推文)页 URL.

        Args:
            parts: ``_split_pid`` 返回的结构化元组.

        Returns:
            str: 单条媒体页 URL.

        Callers:
            - `GalleryDLSource._fetch_detail`
            - `GalleryDLSource.download`
        """
        raise NotImplementedError

    def _split_pid(self, pid: str) -> tuple:
        """把卡片 pid 拆为下载定位所需的结构化元组.

        Args:
            pid: 卡片 id.

        Returns:
            tuple: 结构化元组, 末位为媒体页序号 (用于定位下载文件名).

        Raises:
            ValueError: pid 格式非法.

        Callers:
            - `GalleryDLSource.download`
            - `GalleryDLSource.fetch_meta`
        """
        raise NotImplementedError

    def _post_from_item(self, url: str, kwdict: dict, key: Any = None) -> BooruPost | None:
        """把单条媒体元数据换算为 BooruPost.

        Args:
            url: 媒体 URL.
            kwdict: gallery-dl 产出的媒体元数据字典.
            key: 搜索缓存键; 需要把键编入卡片 id 的来源(如 X)使用.

        Returns:
            BooruPost | None: 非目标媒体或无有效 URL 时返回 None.

        Callers:
            - `GalleryDLSource._ensure_cached`
            - `GalleryDLSource.fetch_meta`
        """
        raise NotImplementedError

    def _caption_from_item(self, kwdict: dict) -> str:
        """从单条媒体元数据提取旁路标签文本.

        Args:
            kwdict: gallery-dl 产出的媒体元数据字典.

        Returns:
            str: 旁路标签文本.

        Callers:
            - `GalleryDLSource.download`
        """
        raise NotImplementedError

    def _auth_overrides(self) -> list[tuple[tuple[str, ...], str, Any]]:
        """构造认证相关的临时配置项, 供 ``config.apply`` 使用.

        Returns:
            list[tuple]: gallery-dl 配置项列表; 未配置认证时为空列表.

        Callers:
            - `GalleryDLSource._gallery_meta_incremental`
        """
        return []

    # ---- 通用脚手架 ----

    def _proxy_overrides(self) -> list[tuple[tuple[str, ...], str, Any]]:
        """构造上游代理的临时配置项, 供 ``config.apply`` 使用.

        代理从 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量读取, 空串或缺失
        表示直连.

        Returns:
            list[tuple]: 形如 ``[((), "proxy", 地址)]`` 的配置项列表.

        Callers:
            - `GalleryDLSource._gallery_meta_incremental`
        """
        p = os.environ.get("HTTP_PROXY", "") or os.environ.get("HTTPS_PROXY", "")
        p = p.strip()
        return [] if not p else [((), "proxy", p)]

    def _gallery_meta_incremental(
        self,
        url: str,
        *,
        cursor: str | None,
        cap: int | None,
    ) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
        """驱动 ``extractor.items()`` 增量拉取媒体元数据.

        迭代 extractor 的消息生成器收集 ``Message.Url``, 达到 ``cap`` 时提前
        ``break`` 并返回当前游标供下次续拉; ``cap`` 为 None 时拉取到游标耗尽
        为止。若 extractor 产出 ``Message.Queue`` 则递归跟随其子 extractor。

        Args:
            url: 搜索页或单条媒体页 URL.
            cursor: 上次拉取的游标, None 表示从开头拉取.
            cap: 单批目标媒体条数, 达到后提前停止; None 表示全量.

        Returns:
            tuple[list[tuple[str, dict]], str | None]:
                (媒体URL/元数据对列表, 下次续拉游标; 拉取完毕时游标为 None).

        Raises:
            RuntimeError: 无可用 extractor 时抛出.

        Callers:
            - `GalleryDLSource._gallery_meta`
            - `GalleryDLSource._ensure_cached`
        """
        overrides = self._auth_overrides() + self._proxy_overrides()
        if cursor:
            overrides.append(((), "cursor", cursor))
        with config.apply(overrides):
            extr = gallery_find(url)
            if extr is None:
                raise RuntimeError(f"无可用 extractor: {url}")

            def collect(extr: Any) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
                """迭代单层 extractor, 递归处理 Queue 消息.

                ``gallery_find`` 返回的 extractor 未初始化, 需先 ``initialize()``
                才能具备 cookies/session 等属性; 部分来源会把媒体队列到子
                extractor, 用 ``Message.Queue`` 递归跟随之.

                Callers:
                    - `GalleryDLSource._gallery_meta_incremental`
                """
                extr.initialize()
                pairs: list[tuple[str, dict[str, Any]]] = []
                for msg in extr.items():
                    if len(msg) < 2:
                        continue
                    if msg[0] == Message.Url and isinstance(msg[2], dict):
                        pairs.append((msg[1], msg[2]))
                        if cap is not None and len(pairs) >= cap:
                            break
                    elif msg[0] == Message.Queue and isinstance(msg[1], str):
                        queued = gallery_find(msg[1])
                        if queued is not None:
                            sub_pairs, _ = collect(queued)
                            pairs.extend(sub_pairs)
                            if cap is not None and len(pairs) >= cap:
                                break
                return pairs, getattr(extr, "_cursor", None)

            pairs, next_cursor = collect(extr)
        return pairs, next_cursor

    def _gallery_meta(
        self, url: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        """用 gallery-dl 全量拉取媒体元数据.

        Args:
            url: 单条媒体页 URL.

        Returns:
            list[tuple[str, dict]]: 每个元素为 (媒体URL, 元数据字典).

        Raises:
            RuntimeError: 无可用 extractor 或拉取失败时抛出.

        Callers:
            - `GalleryDLSource._fetch_detail`
        """
        pairs, _ = self._gallery_meta_incremental(url, cursor=None, cap=None)
        return pairs

    def _fetch_detail(self, parts: tuple) -> list[dict[str, Any]]:
        """同步拉取单条媒体(作品/推文)的元数据列表.

        Args:
            parts: ``_split_pid`` 拆解出的元组.

        Returns:
            list[dict]: 该媒体的元数据列表 (按 num 排序).

        Raises:
            RuntimeError: gallery-dl 拉取失败时抛出.

        Callers:
            - `GalleryDLSource.download`
            - `GalleryDLSource.fetch_meta`
        """
        url = self._detail_url(parts)
        return [dict(kwdict, url=raw_url) for raw_url, kwdict in self._gallery_meta(url)]

    def _ensure_cached(self, key: Any, cache: _Cache | None) -> _Cache:
        """同步拉取一批媒体并追加到缓存.

        从 ``cache.cursor`` 续拉 ``_INCR_BATCH`` 张图片追加到已有列表并返回
        新缓存与续拉游标; ``_INCR_BATCH`` 为 None 时一次性全量拉取.

        Args:
            key: 搜索缓存键.
            cache: 现有缓存; None 表示首次拉取.

        Returns:
            _Cache: 追加增量后的缓存对象.

        Raises:
            RuntimeError: gallery-dl 拉取失败时抛出.

        Callers:
            - `GalleryDLSource.search`
        """
        pairs, next_cursor = self._gallery_meta_incremental(
            self._search_url(key),
            cursor=(cache.cursor if cache else None),
            cap=self._INCR_BATCH,
        )
        new_posts = [
            post
            for raw_url, kwdict in pairs
            if (post := self._post_from_item(raw_url, kwdict, key)) is not None
        ]
        prev_posts = cache.posts if cache else []
        return _Cache(posts=prev_posts + new_posts, cursor=next_cursor)

    async def search(
        self,
        tags: str,
        page: int,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool]:
        """游标式来源不支持页码式搜索; 使用 ``search_session``. 实现 ``base.SourceClient.search``.

        Callers:
            - `base.py:search_posts` (仅页码式来源触发)
        """
        raise NotImplementedError("gallerydl 为会话式来源, 请使用 search_session")

    async def search_session(
        self,
        tags: str,
        session: str,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool, str]:
        """按会话续拉下一批媒体, 返回 (posts, has_more, session_token).

        会话持有已拉缓存与续拉游标, 每次调用从游标续拉一批并返回该批新增
        卡片 (``_INCR_BATCH`` 条; None 时全量拉取一次返回全部)。末端页仅向下:
        已返回的卡片由前端累计, 不再重发。发起新搜索 (session 为空或查询键
        变化) 或会话超过一天无翻页时由 ``SessionStore`` 重建会话。

        Callers:
            - `routes/online_sources.py:search`
        """
        key = self._search_key(tags, extra)
        if key is None:
            return [], False, ""
        sess = await self._sessions.acquire(session, self.name, key)
        async with sess.lock:
            st = sess.state
            if not st:
                st.update(posts=[], cursor=_NOT_STARTED, returned=0)
            if st["cursor"] is _NOT_STARTED:
                cache = await asyncio.to_thread(self._ensure_cached, key, None)
                st["posts"] = cache.posts
                st["cursor"] = cache.cursor
            elif st["cursor"] is not None:
                cache = await asyncio.to_thread(
                    self._ensure_cached,
                    key,
                    _Cache(posts=st["posts"], cursor=st["cursor"]),
                )
                st["posts"] = cache.posts
                st["cursor"] = cache.cursor

            batch = self._INCR_BATCH
            if batch:
                new_posts = st["posts"][st["returned"] : st["returned"] + batch]
                st["returned"] += len(new_posts)
            else:
                new_posts = st["posts"][st["returned"]:]
                st["returned"] = len(st["posts"])
        has_more = st["cursor"] is not None or st["returned"] < len(st["posts"])
        return new_posts, has_more, sess.token

    async def download(
        self,
        post: BooruPost,
        out_dir: Path,
        *,
        overwrite: bool = False,
        anima_format: bool = True,
        include_meta_tags: bool = False,
        keep_underscores: bool = False,
        emit: ProgressFn | None = None,
    ) -> dict:
        """下载指定媒体为 PNG, 并写旁路标签与 meta.

        直接使用 search 阶段已持有的 ``post.file_url`` (原图 URL) 与
        ``post.tags`` 写入磁盘, 不再重新请求帖文元数据。gallery-dl 来源为
        纯图片, ``emit`` 透传但不起作用。

        Args:
            emit: 下载进度回调, 图片来源忽略 (透传签名统一).

        Callers:
            - `base.py:SourceClient.download` (经 ``media.download_post`` 委托)
        """
        pid = post.id
        stem = Path(post.filename).stem
        image_path = out_dir / post.filename
        tags_path = out_dir / f"{stem}.txt"
        existing = find_downloaded(out_dir, post.filename)

        if not overwrite and existing is not None and (tags_path.is_file() or not self.writes_caption):
            return {
                "skipped": True,
                "path": str(existing),
                "stem": stem,
                "id": pid,
                "tags_path": str(tags_path),
            }

        image_url = (post.file_url or "").strip()
        if not image_url:
            raise ValueError(f"pid {pid} 无媒体 URL")
        caption = (post.tags or "").strip()
        if include_meta_tags:
            caption = (caption + (" " + self.name) if caption else self.name).strip()
        async with HttpClient() as client:
            data, _ = await client.get_bytes(image_url)

        await self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        if self.writes_caption:
            tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(
            out_dir, pid,
            BooruPost(source=self.name, id=pid, file_url=image_url, tags=caption),
        )
        return {
            "path": str(image_path),
            "stem": stem,
            "id": pid,
            "tags_path": str(tags_path),
        }

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """按 pid 拉取媒体元数据并写入磁盘.

        Callers:
            - `routes/online_sources.py:_run_refresh`
        """
        parts = self._split_pid(pid)
        num = parts[-1]
        items = await asyncio.to_thread(self._fetch_detail, parts)
        target = next((it for it in items if it.get("num") == num), None)
        if target is None:
            raise ValueError(f"pid {pid} 无第 {num} 张媒体")
        post = self._post_from_item(str(target.get("url") or ""), target)
        if post is None:
            raise ValueError(f"第 {num} 张媒体的类型不支持")
        post.id = pid
        self._write_meta(out_dir, pid, post)