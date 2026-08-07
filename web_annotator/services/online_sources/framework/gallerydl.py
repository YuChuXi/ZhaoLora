"""gallery-dl 驱动的来源客户端基类.

把 gallery-dl 共用的脚手架 (元数据拉取、下载到临时目录、媒体定位、
增量分页缓存、旁路标签与 meta 写入) 统一到 ``GalleryDLSource``。来源
子类只需实现来源特有的查询键、URL 构造、元数据换算与认证配置, 即可
获得完整的 ``search`` / ``download`` / ``fetch_meta`` 能力。

Callers:
    - ``services.online_sources.pixiv``
    - ``services.online_sources.x``
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gallery_dl import config, job
from gallery_dl.extractor import find as gallery_find
from gallery_dl.extractor.message import Message

from services.online_sources.framework.base import SourceClient
from services.online_sources.framework.utils import find_downloaded
from services.models import BooruPost


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

    子类可覆盖 ``_IMG_EXT``(媒体扩展名白名单)、``_PAGE_SIZE``(每页卡片数)、
    ``_INCR_BATCH``(单批增量条数; None 表示一次性全量拉取)。
    """

    # 子类可覆盖
    _IMG_EXT: frozenset[str] = frozenset()
    _PAGE_SIZE = 40
    _INCR_BATCH: int | None = None

    def __init__(self) -> None:
        """初始化来源级的缓存、锁与并发保护。"""
        self._cache: dict[Any, _Cache] = {}
        self._locks: dict[Any, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

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
            - `GalleryDLSource._download_to_dir`
        """
        return []

    # ---- 通用脚手架 ----

    def _proxy_overrides(self, proxy: str | None) -> list[tuple[tuple[str, ...], str, Any]]:
        """构造上游代理的临时配置项, 供 ``config.apply`` 使用.

        Args:
            proxy: 代理地址; 空串或 None 表示直连.

        Returns:
            list[tuple]: 形如 ``[((), "proxy", 地址)]`` 的配置项列表.

        Callers:
            - `GalleryDLSource._gallery_meta_incremental`
            - `GalleryDLSource._download_to_dir`
        """
        p = (proxy or "").strip()
        return [] if not p else [((), "proxy", p)]

    def _gallery_meta_incremental(
        self,
        url: str,
        cursor: str | None,
        cap: int | None,
        proxy: str | None = None,
    ) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
        """驱动 ``extractor.items()`` 增量拉取媒体元数据.

        迭代 extractor 的消息生成器收集 ``Message.Url``, 达到 ``cap`` 时提前
        ``break`` 并返回当前游标供下次续拉; ``cap`` 为 None 时拉取到游标耗尽
        为止。若 extractor 产出 ``Message.Queue`` 则递归跟随其子 extractor。

        Args:
            url: 搜索页或单条媒体页 URL.
            cursor: 上次拉取的游标, None 表示从开头拉取.
            cap: 单批目标媒体条数, 达到后提前停止; None 表示全量.
            proxy: 上游代理, None 表示直连.

        Returns:
            tuple[list[tuple[str, dict]], str | None]:
                (媒体URL/元数据对列表, 下次续拉游标; 拉取完毕时游标为 None).

        Raises:
            RuntimeError: 无可用 extractor 时抛出.

        Callers:
            - `GalleryDLSource._gallery_meta`
            - `GalleryDLSource._ensure_cached`
        """
        overrides = self._auth_overrides() + self._proxy_overrides(proxy)
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
        self, url: str, proxy: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """用 gallery-dl 全量拉取媒体元数据.

        Args:
            url: 单条媒体页 URL.
            proxy: 上游代理, None 表示直连.

        Returns:
            list[tuple[str, dict]]: 每个元素为 (媒体URL, 元数据字典).

        Raises:
            RuntimeError: 无可用 extractor 或拉取失败时抛出.

        Callers:
            - `GalleryDLSource._fetch_detail`
        """
        pairs, _ = self._gallery_meta_incremental(url, cursor=None, cap=None, proxy=proxy)
        return pairs

    def _fetch_detail(self, parts: tuple, proxy: str | None) -> list[dict[str, Any]]:
        """同步拉取单条媒体(作品/推文)的元数据列表.

        Args:
            parts: ``_split_pid`` 拆解出的元组.
            proxy: 上游代理, None 表示直连.

        Returns:
            list[dict]: 该媒体的元数据列表 (按 num 排序).

        Raises:
            RuntimeError: gallery-dl 拉取失败时抛出.

        Callers:
            - `GalleryDLSource.download`
            - `GalleryDLSource.fetch_meta`
        """
        url = self._detail_url(parts)
        return [dict(kwdict, url=raw_url) for raw_url, kwdict in self._gallery_meta(url, proxy)]

    def _download_to_dir(self, url: str, tmp_dir: Path, proxy: str | None) -> None:
        """同步用 gallery-dl 下载整条媒体到临时目录.

        覆盖 filename 为 ``{num}.{extension}``, 使下载文件名带媒体序号,
        便于按 ``num`` 定位目标图片.

        Args:
            url: 单条媒体页 URL.
            tmp_dir: 临时下载目录.
            proxy: 上游代理, None 表示直连.

        Returns:
            None

        Raises:
            RuntimeError: gallery-dl 下载失败时抛出.

        Callers:
            - `GalleryDLSource.download`
        """
        overrides = self._auth_overrides() + [
            ((), "base-directory", str(tmp_dir)),
            ((), "directory", ()),
            ((), "filename", "{num}.{extension}"),
            (("output",), "mode", False),
        ]
        if proxy:
            overrides.append(((), "proxy", proxy))
        with config.apply(overrides):
            djob = job.DownloadJob(url)
            djob.run()
        if djob.status != 0:
            raise RuntimeError(f"gallery-dl 下载失败(状态 {djob.status})")

    def _pick_media(self, tmp_dir: Path, num: int) -> bytes:
        """从下载目录读取第 num 张图片媒体内容.

        Args:
            tmp_dir: 下载目录, 内含 ``{num}.{ext}`` 命名的媒体文件.
            num: 媒体序号.

        Returns:
            bytes: 图片内容.

        Raises:
            FileNotFoundError: 对应媒体文件缺失.

        Callers:
            - `GalleryDLSource.download`
        """
        for ext in self._IMG_EXT:
            p = tmp_dir / f"{num}.{ext}"
            if p.is_file():
                return p.read_bytes()
        raise FileNotFoundError(f"未找到第 {num} 张媒体")

    def _ensure_cached(self, key: Any, cache: _Cache | None, proxy: str | None) -> _Cache:
        """同步拉取一批媒体并追加到缓存.

        从 ``cache.cursor`` 续拉 ``_INCR_BATCH`` 张图片追加到已有列表并返回
        新缓存与续拉游标; ``_INCR_BATCH`` 为 None 时一次性全量拉取.

        Args:
            key: 搜索缓存键.
            cache: 现有缓存; None 表示首次拉取.
            proxy: 上游代理, None 表示直连.

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
            proxy=proxy,
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
        proxy: str | None,
    ) -> tuple[list[BooruPost], bool]:
        """按查询键搜索, 返回 (posts, has_more).

        当请求页超出已缓存数且来源支持续拉时增量拉取(游标续拉或全量), 之后
        从缓存按页切片返回; 页大小固定为 ``_PAGE_SIZE``。

        Callers:
            - `base.py:search_posts` / `routes/booru.py:booru_search`
        """
        key = self._search_key(tags, extra)
        if key is None:
            return [], False

        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock

        need = max(page, 0) * self._PAGE_SIZE
        async with lock:
            cache = self._cache.get(key)
            if cache is None or (cache.cursor is not None and len(cache.posts) < need):
                cache = await asyncio.to_thread(self._ensure_cached, key, cache, proxy)
                self._cache[key] = cache

        posts = cache.posts
        start = (page - 1) * self._PAGE_SIZE
        slice_posts = posts[start : start + self._PAGE_SIZE]
        has_more = start + self._PAGE_SIZE < len(posts)
        return slice_posts, has_more

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
        """下载指定媒体为 PNG, 并写旁路标签与 meta.

        Callers:
            - `base.py:SourceClient.download` (经 ``media.download_post`` 委托)
        """
        parts = self._split_pid(pid)
        num = parts[-1]

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

        items = await asyncio.to_thread(self._fetch_detail, parts, proxy)
        target = next((it for it in items if it.get("num") == num), None)
        if target is None:
            raise ValueError(f"pid {pid} 无第 {num} 张媒体")

        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            await asyncio.to_thread(self._download_to_dir, self._detail_url(parts), tmp_dir, proxy)
            data = await asyncio.to_thread(self._pick_media, tmp_dir, num)

        self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        caption = self._caption_from_item(target)
        if include_meta_tags:
            caption = (caption + (" " + self.name) if caption else self.name).strip()
        tags_path.write_text(caption + "\n", encoding="utf-8")
        self._write_meta(
            out_dir,
            pid,
            BooruPost(source=self.name, id=pid, file_url=str(target.get("url") or ""), tags=caption),
        )
        return {
            "path": str(image_path),
            "stem": stem,
            "id": pid,
            "tags_path": str(tags_path),
        }

    async def fetch_meta(self, out_dir: Path, pid: str, proxy: str | None) -> None:
        """按 pid 拉取媒体元数据并写入磁盘.

        Callers:
            - `routes/booru.py:_run_refresh`
        """
        parts = self._split_pid(pid)
        num = parts[-1]
        items = await asyncio.to_thread(self._fetch_detail, parts, proxy)
        target = next((it for it in items if it.get("num") == num), None)
        if target is None:
            raise ValueError(f"pid {pid} 无第 {num} 张媒体")
        post = self._post_from_item(str(target.get("url") or ""), target)
        if post is None:
            raise ValueError(f"第 {num} 张媒体的类型不支持")
        post.id = pid
        self._write_meta(out_dir, pid, post)