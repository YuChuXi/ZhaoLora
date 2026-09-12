"""B 站来源客户端: 将用户动态的每张图映射为一张 BooruPost 卡片.

`tags` 参数承载 B 站 UID, `extra` 为筛选串 (``type:`` / ``after:`` /
``before:`` / ``keyword:``)。搜索时先分页拉取该用户全部动态并解析, 按
筛选条件过滤后把所有可展示图片展开为扁平列表, 缓存于进程内并按其序号
分页返回; 下载时按 ``{动态ID}_{图片序号}`` 的 pid 解析原图 URL 并写入磁盘。

Callers:
    - ``services/online_sources/__init__.py:`` (注册)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime
from pathlib import Path

from services.core.video_scanner import VIDEO_EXTS
from services.online_sources.framework.base import SourceClient
from services.online_sources.bilibili.client import (
    fetch_bili_bytes,
    fetch_dynamics,
    resolve_record,
    fetch_video_record,
)
from services.online_sources.bilibili.models import DynamicRecord, SessionState
from services.online_sources.framework.session import SessionStore
from services.online_sources.framework.utils import find_downloaded, probe_image_size

from services.models import BooruPost

logger = logging.getLogger(__name__)

# 动态形态类别判定
def _record_type(record: DynamicRecord) -> str:
    """判定动态的内容形态类别.

    转发视频动态的内容本体是视频, 与原创视频同归 ``video`` (由视频源
    处理, 展开时携带 ``is_forward`` 标记); 其余转发归 ``forward``,
    图片动态归 ``image``, 后两者由动态图源处理。

    Args:
        record (DynamicRecord): 单条动态记录.

    Returns:
        str: ``"image"`` / ``"video"`` / ``"forward"`` 三者之一。

    Callers:
        - ``services/online_sources/bilibili/source.py:BilibiliSource._matches``
    """
    if record.forwarded is not None:
        fwd = record.forwarded
        return "video" if (fwd.video and fwd.video.bvid) else "forward"
    if record.video and record.video.bvid:
        return "video"
    return "image"


def _pub_iso(pub_ts: int | str | None) -> str:
    """把 B 站动态发布时间戳转为本地时区 ISO8601 文本.

    Args:
        pub_ts (int | str | None): 动态发布时间戳 (desktop 接口为 int,
            v1 接口为字符串), 缺失时返回空串。

    Returns:
        str: ISO8601 本地时区时间文本, 无时间戳时为空串。

    Callers:
        - ``services/online_sources/bilibili/source.py:BilibiliSource._expand``
    """
    if not pub_ts:
        return ""
    return datetime.fromtimestamp(int(pub_ts)).astimezone().isoformat()


async def _iter_console_lines(stdout: asyncio.StreamReader) -> AsyncIterator[str]:
    """按 ``\\r`` 与 ``\\n`` 双分隔符切分子进程控制台输出为文本行.

    BBDown 的下载进度条以 ``\\r`` 单行刷新且不输出换行, ``readline`` 会把
    整个下载阶段的输出阻塞为一次超长读取; 此处按块读取并即时切分, 使
    进度条每一帧都能实时到达解析方。

    Args:
        stdout (asyncio.StreamReader): 子进程标准输出流 (stderr 已并入).

    Yields:
        str: 去除首尾空白的单行文本, 无效编码字节以替换符处理。

    Callers:
        - ``services/online_sources/bilibili/source.py:BilibiliSource._download_video``
    """
    sep = re.compile(rb"\r\n|\r|\n")
    buf = b""
    while True:
        chunk = await stdout.read(4096)
        if not chunk:
            break
        parts = sep.split(buf + chunk)
        buf = parts.pop()
        for part in parts:
            if part:
                yield part.decode("utf-8", "replace").strip()
    if buf:
        yield buf.decode("utf-8", "replace").strip()


class BilibiliSource(SourceClient):
    """B 站动态图源: 从用户动态拉取图片帖与转发帖.

    本源仅产出 ``image`` / ``forward`` 两类帖子; 视频动态 (含转发视频)
    由同家族的 ``BilibiliVideoSource`` 处理, 两源共享动态拉取与查询解析。

    Callers:
        - ``services/online_sources/framework/base.py:register_source``
        - ``services/online_sources/bilibili/source.py:BilibiliVideoSource`` (基类)
    """

    name = "bilibili"
    prefix = "bi"
    order_tags: dict[str, str] = {}
    valid_orders: tuple[str, ...] = ()
    session_based = True
    writes_caption = False
    image_hosts = ("hdslb.com",)
    image_referer = "https://www.bilibili.com/"
    # 本源产出的动态形态类别 (类型筛选的默认全集)
    POST_TYPES: frozenset[str] = frozenset({"image", "forward"})

    def __init__(self) -> None:
        """初始化会话存储. """
        super().__init__()
        self._sessions = SessionStore()

    def parse_query(self, tags: str, extra: str) -> dict[str, str]:
        """解析 B 站查询串: ``tags`` 为 UID, ``extra`` 为 ``type:``/``after:``/``before:``/``keyword:``.

        类型可多选, 以多个 ``type:`` token 表达, 结果以逗号分隔去重保序
        (字符串, 可直接用作分页缓存键)。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search``
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
        """判断动态是否归属本源并满足关键词 / 时间筛选.

        大类归属按动态形态类别与本源 ``POST_TYPES`` 判定 (视频动态不进
        动态图源, 反之亦然); 关键词 / 时间在此 (动态层) 判定。帖子形态
        类型的筛选 (image/forward/video/cover) 在展开后的帖子上执行,
        见 ``_post_type_match``。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search_session``
        """
        if _record_type(record) not in self.POST_TYPES:
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

    def _post_type_match(self, post: BooruPost, f: dict[str, str]) -> bool:
        """判断帖子的形态类型是否满足 ``type:`` 筛选.

        同一视频动态会展开出视频帖与封面帖两帖, 类型筛选必须在帖子层
        执行才能分别选中; 请求为空时全部命中。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search_session``
        """
        return not f["type"] or post.post_type in f["type"].split(",")

    def _expand(self, record: DynamicRecord) -> list[BooruPost]:
        """将一条动态展开为图片帖.

        转发动态取原文的图 (帖子携带 ``is_forward`` 并标 ``forward`` 类);
        视频动态不经过本源 (由 ``BilibiliVideoSource`` 展开)。帖子 id 用
        外层 ``record.id``, 避免原动态与转发动态并存时展开出相同 id;
        缩略图 / 样图 URL 由原图 URL 加 B 站 CDN 后缀派生。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search_session``
        """
        source = record.forwarded if record.forwarded is not None else record
        text = source.text or record.text
        if not source.images:
            return []
        return [
            BooruPost(
                source=self.name,
                id=f"{record.id}_{i}",
                post_type="forward" if record.forwarded is not None else "image",
                width=w,
                height=h,
                posted_at=_pub_iso(source.pub_ts),
                file_url=url,
                preview_url=url + "@480w_480h_1e.webp",
                sample_url=url + "@1280w_1280h_1e.webp",
                tags=text,
                is_forward=record.forwarded is not None,
            )
            for i, (url, w, h) in enumerate(
                (img.url, img.width or 0, img.height or 0) for img in source.images
            )
        ]

    async def search(
        self,
        tags: str,
        page: int,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool]:
        """游标式来源不支持页码式搜索; 使用 ``search_session``. 实现 ``base.SourceClient.search``.

        Callers:
            - ``services/online_sources/framework/base.py:search_posts`` (仅页码式来源触发)
        """
        raise NotImplementedError("bilibili 为会话式来源, 请使用 search_session")

    async def search_session(
        self,
        tags: str,
        session: str,
        order: str,
        extra: str,
    ) -> tuple[list[BooruPost], bool, str]:
        """按会话续拉下一批动态图片, 返回 (posts, has_more, session_token).

        B 站 feed 是游标分页, 会话持有动态游标; 每次调用从当前游标拉取
        下一批动态并过滤展开, 直到凑出非空批次或上游耗尽。发起新搜索
        (session 为空或查询键变化) 或会话超过一天无翻页时由 ``SessionStore``
        重建会话。

        风控/限流时接口返回空心响应 (无记录且无游标), 与真实末页结构一致、
        无法单次区分; 会话**不锁存**末页判定: 空心页直接返回空结果且不推进
        游标, 由有记忆的调用方 (批量下载) 连续空页确认后才判定抓完, 避免
        瞬时风控导致批量提前停止。

        Callers:
            - ``routes/online_sources.py:search``
        """
        f = self.parse_query(tags, extra)
        uid = int(f["uid"])
        query_key = (uid, f["type"], f["after"], f["before"], f["keyword"])
        sess = await self._sessions.acquire(session, self.name, query_key)
        async with sess.lock:
            st = sess.state
            if st is None:
                st = SessionState()
                sess.state = st
            page_posts: list[BooruPost] = []
            has_more = False
            retries = 0
            while not page_posts:
                records, next_offset, has_more = await fetch_dynamics(
                    uid, offset=st.offsets[-1]
                )
                if not records and not next_offset:
                    if retries < 3:
                        # 风控空心响应: 短暂等待后重试, 最多 3 次
                        retries += 1
                        await asyncio.sleep(1.5)
                        continue
                    # 连续 3 次空心: 交由上层判定
                    return [], False, sess.token
                if next_offset and next_offset != st.offsets[-1]:
                    st.offsets.append(next_offset)
                page_posts = [
                    post
                    for record in records
                    if self._matches(record, f)
                    for post in self._expand(record)
                    if self._post_type_match(post, f)
                ]
            page_posts = await self._post_process(page_posts)
        return page_posts, has_more, sess.token

    async def _post_process(self, posts: list[BooruPost]) -> list[BooruPost]:
        """搜索批次产出后的补全钩子, 基类默认原样返回.

        Args:
            posts (list[BooruPost]): 本批展开后的帖子。

        Returns:
            list[BooruPost]: 补全后的帖子。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search_session``
        """
        return posts

    def split(self, stem: str) -> str:
        """从 stem 拆出本来源 pid, 非本来源格式抛 ValueError.

        pid 为 ``{动态ID}_{序号}`` 文本, 经 ``_split_pid`` 校验.

        Callers:
            - ``services/online_sources/framework/base.py:detect_source``
        """
        rest = stem[len(self.prefix):] if stem.startswith(self.prefix) else ""
        self._split_pid(rest)
        return rest

    def _split_pid(self, pid: str) -> tuple[str, int]:
        """将 ``{动态ID}_{图片序号}`` 拆为 (动态ID, 序号).

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.download``
            - ``services/online_sources/bilibili/source.py:BilibiliSource.fetch_meta``
        """
        idx = pid.rfind("_")
        if idx <= 0:
            raise ValueError(f"非法 id: {pid}")
        return pid[:idx], int(pid[idx + 1:])

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """按 pid 重新解析并写入帖元数据.

        图片帖复用 ``_expand`` 展开逻辑, 以与搜索阶段同源的完整字段 (发布
        时间 / 宽高 / 缩略图派生 URL) 重写 meta。实现
        ``base.SourceClient.fetch_meta``; 视频 pid 的元数据重建由
        ``BilibiliVideoSource`` 覆盖实现。

        Args:
            out_dir (Path): 数据集工作目录。
            pid (str): ``{动态ID}_{序号}`` (图片帖)。

        Raises:
            ValueError: pid 不合法。

        Callers:
            - ``routes/online_sources.py:_run_refresh``
            - ``services/online_sources/bilibili/source.py:BilibiliVideoSource.fetch_meta`` (经 ``super()`` 调用图片分支骨架)
        """
        dynamic_id, index = self._split_pid(pid)
        record = await resolve_record(dynamic_id)
        self._write_meta(out_dir, pid, self._expand(record)[index])

    async def download(
        self,
        post: BooruPost,
        out_dir: Path,
        *,
        overwrite: bool = False,
        anima_format: bool = True,
        include_meta_tags: bool = False,
        keep_underscores: bool = False,
        emit: Callable[[int, int], None] | None = None,
    ) -> dict:
        """下载图片帖到 ``out_dir``.

        按 ``post.file_url`` 转 PNG 写入磁盘, 元数据以入参 ``post`` 写入
        meta, 其中宽高以转码后的实际像素尺寸为准 (B 站动态 API 不提供
        尺寸, 展开阶段的 0 值与后续重建值均由产物修正)。实现
        ``base.SourceClient.download``; 视频帖下载由 ``BilibiliVideoSource``
        覆盖实现。

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.download`` (经 ``media.download_post`` 委托)
            - ``services/online_sources/batch.py:download_one``
            - ``services/online_sources/bilibili/source.py:BilibiliVideoSource.download`` (经 ``super()`` 调用图片分支)
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

        url = (post.file_url or "").strip()
        if not url:
            raise ValueError(f"pid {pid} 无媒体 URL")
        data, _ = await fetch_bili_bytes(url)
        width, height = await self._save_png(data, image_path)
        self._clean_old_files(out_dir, pid, image_path)
        if (post.width, post.height) != (width, height):
            post = post.model_copy(update={"width": width, "height": height})
        self._write_meta(out_dir, pid, post)
        return {
            "path": str(image_path),
            "stem": stem,
            "id": pid,
            "tags_path": str(tags_path),
        }


class BilibiliVideoSource(BilibiliSource):
    """B 站视频源: 从用户动态拉取视频动态, 展开为封面帖与视频帖.

    每条视频动态 (含转发视频) 展开为两个帖子: 封面帖 (``cover`` 类,
    普通图片帖形态, 封面直接取视频 cover URL, 产物落工作目录) 与
    视频帖 (``video`` 类, 经 BBDown 下载视频本体, 产物落数据集
    ``video`` 目录)。两帖共用 BV 号作为 id, 以 ``post_type`` 区分
    (会话 key 与类型筛选), 与动态图源 ``BilibiliSource`` 完全分离。

    Callers:
        - ``services/online_sources/framework/base.py:register_source``
    """

    name = "bilibili-video"
    prefix = "bv"
    POST_TYPES: frozenset[str] = frozenset({"video"})

    def __init__(self) -> None:
        """初始化视频源, 建立分辨率缓存 (bvid -> (宽, 高)).

        B 站动态 feed 接口不提供分辨率字段: 搜索批次对视频帖经视频
        信息接口补视频画面分辨率, 对封面帖拉取封面字节用 PIL 实测封面
        尺寸; 缓存避免翻页与重搜的重复请求。
        """
        super().__init__()
        self._dim_cache: dict[str, tuple[int, int]] = {}
        self._cover_dim_cache: dict[str, tuple[int, int]] = {}

    async def _fill_dims(self, posts: list[BooruPost]) -> list[BooruPost]:
        """并发补全视频帖与封面帖的分辨率 (带进程级缓存).

        动态 feed 接口的 archive 卡片不提供分辨率字段 (全字段通读确
        认)。视频帖取视频信息接口 (``fetch_video_record``) 顶层
        ``dimension`` 的视频画面分辨率; 封面帖的分辨率语义是封面图实
        际尺寸 (B 站封面按视频画幅生成但不保证逐像素一致), 拉取封面
        原图字节用 PIL 实测, 与 ``fetch_meta`` 的探测一致。封面尺寸
        无接口提供, 实测是唯一精确来源; 缓存命中的 bvid 不重复请求,
        两类共享并发上限 5, 单条失败跳过该帖补全 (前端以 16:9 平台
        画幅占位)。

        Args:
            posts (list[BooruPost]): 本批展开后的帖子。

        Returns:
            list[BooruPost]: 分辨率已补全的帖子 (缺失原样)。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliVideoSource._post_process``
        """
        video_posts = [p for p in posts if p.post_type == "video"]
        cover_posts = [p for p in posts if p.post_type == "cover"]
        missing_video = sorted({p.id for p in video_posts if p.id not in self._dim_cache})
        missing_cover = sorted({p.id for p in cover_posts if p.id not in self._cover_dim_cache})
        sem = asyncio.Semaphore(5)

        async def load_video(bvid: str) -> None:
            """单条拉取视频信息并写入缓存, 失败不缓存由下次重试."""
            async with sem:
                info = await fetch_video_record(bvid)
            if info.width and info.height:
                self._dim_cache[bvid] = (info.width, info.height)

        async def load_cover(bvid: str, cover: str) -> None:
            """单条拉取封面原图并实测尺寸写入缓存, 失败不缓存由下次重试."""
            async with sem:
                data, _ = await fetch_bili_bytes(cover)
            width, height = probe_image_size(data)
            self._cover_dim_cache[bvid] = (width, height)

        cover_by_id: dict[str, str] = {}
        for post in cover_posts:
            cover_by_id.setdefault(post.id, post.file_url)
        results = await asyncio.gather(
            *(load_video(b) for b in missing_video),
            *(load_cover(b, cover_by_id[b]) for b in missing_cover),
            return_exceptions=True,
        )
        for exc in results:
            if isinstance(exc, Exception):
                logger.warning("视频分辨率补全失败: %s", exc)
        updated: list[BooruPost] = []
        for post in posts:
            if post.post_type == "video" and post.id in self._dim_cache:
                width, height = self._dim_cache[post.id]
                post = post.model_copy(update={"width": width, "height": height})
            elif post.post_type == "cover" and post.id in self._cover_dim_cache:
                width, height = self._cover_dim_cache[post.id]
                post = post.model_copy(update={"width": width, "height": height})
            updated.append(post)
        return updated

    async def _post_process(self, posts: list[BooruPost]) -> list[BooruPost]:
        """搜索批次补全: 为视频帖与封面帖补分辨率.

        Args:
            posts (list[BooruPost]): 本批展开后的帖子。

        Returns:
            list[BooruPost]: 补全后的帖子。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search_session``
        """
        return await self._fill_dims(posts)

    def _expand(self, record: DynamicRecord) -> list[BooruPost]:
        """将一条视频动态展开为封面帖与视频帖.

        封面帖直接取视频 cover URL 作为媒体, 缩略图 / 样图 URL 由封面
        URL 加 B 站 CDN 后缀派生; 视频帖携带 BV 号供 BBDown 下载,
        ``file_url`` 同为封面 (仅供前端展示)。动态 API 不提供封面像素
        尺寸, ``width`` / ``height`` 置 0, 由 ``_post_process`` 实测
        封面补全 (失败时前端以 16:9 平台画幅占位); 无 BV 号或封面的
        动态不产出帖子。

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliSource.search_session``
        """
        source = record.forwarded if record.forwarded is not None else record
        text = source.text or record.text
        bvid = (source.video.bvid or "") if source.video else ""
        cover = (source.video.cover or "") if source.video else ""
        if not bvid or not cover:
            return []
        preview = cover + "@480w_480h_1e.webp"
        sample = cover + "@1280w_1280h_1e.webp"
        return [
            BooruPost(
                source=self.name,
                id=bvid,
                post_type="cover",
                posted_at=_pub_iso(source.pub_ts),
                file_url=cover,
                preview_url=preview,
                sample_url=sample,
                tags=text,
                is_forward=record.forwarded is not None,
            ),
            BooruPost(
                source=self.name,
                id=bvid,
                post_type="video",
                is_video=True,
                posted_at=_pub_iso(source.pub_ts),
                file_url=cover,
                preview_url=preview,
                sample_url=sample,
                tags=text,
                is_forward=record.forwarded is not None,
            ),
        ]

    def split(self, stem: str) -> str:
        """从 stem 拆出本来源 pid, 非本来源格式抛 ValueError.

        pid 为 BV 号 (stem 即 ``bv`` 前缀 + BV 号), 需以 ``BV`` 开头。

        Callers:
            - ``services/online_sources/framework/base.py:detect_source``
        """
        rest = stem[len(self.prefix):] if stem.startswith(self.prefix) else ""
        if not rest.startswith("BV"):
            raise ValueError(f"非法 id: {stem}")
        return rest

    async def fetch_meta(self, out_dir: Path, pid: str) -> None:
        """按 pid (BV 号) 经视频信息接口重建封面帖元数据.

        封面帖元数据以视频封面为媒体, 字段语义与 ``_expand`` 的封面帖
        一致 (简介作标签 / 发布时间), ``is_video`` 为 False; 宽高经拉取
        封面字节用 PIL 实测 (动态 API 不提供尺寸)。实现覆盖
        ``BilibiliSource.fetch_meta``。

        Args:
            out_dir (Path): 数据集工作目录。
            pid (str): BV 号。

        Raises:
            ValueError: 视频无封面。

        Callers:
            - ``routes/online_sources.py:_run_refresh``
        """
        video = await fetch_video_record(pid)
        cover = video.cover or ""
        if not cover:
            raise ValueError(f"视频 {pid} 无封面")
        data, _ = await fetch_bili_bytes(cover)
        width, height = probe_image_size(data)
        self._write_meta(out_dir, pid, BooruPost(
            source=self.name,
            id=pid,
            post_type="cover",
            width=width,
            height=height,
            posted_at=_pub_iso(video.pub_ts or 0),
            file_url=cover,
            preview_url=cover + "@480w_480h_1e.webp",
            sample_url=cover + "@1280w_1280h_1e.webp",
            tags=video.desc or video.title or "",
        ))

    async def download(
        self,
        post: BooruPost,
        out_dir: Path,
        *,
        overwrite: bool = False,
        anima_format: bool = True,
        include_meta_tags: bool = False,
        keep_underscores: bool = False,
        emit: Callable[[int, int], None] | None = None,
    ) -> dict:
        """下载帖子: 视频帖经 BBDown, 封面帖复用图片分支.

        调用方保证 ``out_dir`` 语义正确: 视频帖传入数据集根 ``video``
        目录 (BBDown 产物原位写入), 封面帖传入筛选工作目录。

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.download`` (经 ``media.download_post`` 委托)
            - ``services/online_sources/batch.py:download_one``
        """
        if post.is_video:
            return await self._download_video(post, out_dir, emit=emit)
        return await super().download(
            post, out_dir,
            overwrite=overwrite,
            anima_format=anima_format,
            include_meta_tags=include_meta_tags,
            keep_underscores=keep_underscores,
        )

    async def _download_video(
        self,
        post: BooruPost,
        video_dir: Path,
        *,
        emit: Callable[[int, int], None] | None = None,
    ) -> dict:
        """调用 BBDown 下载视频帖子(BV 号)到 video 目录, 返回写入结果.

        B 站 DASH 音视频混流由专用工具 BBDown 完成, 本层仅委托子进程并
        解析控制台输出回传聚合进度。BBDown 路径取自环境变量 ``BBDOWN_PATH``,
        缺省时在 PATH 中查找 ``BBDown``; WEB 扫码登录凭证由 BBDown 自行从
        其可执行文件同目录的 ``BBDown.data`` 读取, 会员/高清内容依赖该登录态。
        下载以 ``post.id`` (即 BV 号) 为输入, 输出到 ``video_dir`` (数据集根
        ``video``): 产物一律平铺于该目录顶层, 单 P 命名为 ``{bvid}_{视频标题}``,
        多 P 命名为 ``{bvid}_{视频标题}_P{补零序号}_{分P标题}``, BV 前缀保证
        不同视频同名时不互相覆盖。多 P 视频逐分 P 顺序下载, 进度按下载序号
        与总分 P 数聚合为整体百分比。清晰度与编码优先级分别经环境变量
        ``BBDOWN_DFN_PRIORITY`` / ``BBDOWN_ENCODING_PRIORITY`` 传入
        (BBDown 的 ``-q`` / ``-e``), 留空即 BBDown 默认 (清晰度最高优先)。

        Args:
            post: 视频帖子(``id`` 即 BV 号).
            video_dir: 输出目录(数据集根 ``video``).
            emit: 进度回调 ``(pct, total)``, 总量为 100 (聚合后的整体百分比);
                为 None 时不回传进度.

        Returns:
            dict: 含 ``path``/``stem``/``id`` (首个分 P 产物) 与 ``files``
                (全部产物文件名列表), 供 ``media.download_post`` 包装;
                ``overwrite`` 为 False 且该 BV 产物已存在时返回
                ``skipped=True`` 及既有首个文件。

        Raises:
            ValueError: 缺少 bvid / 未找到 BBDown 工具 / 未产出文件.
            RuntimeError: BBDown 退出码非 0, 消息附带其控制台日志尾部.

        Callers:
            - ``services/online_sources/bilibili/source.py:BilibiliVideoSource.download``
        """
        bvid = (post.id or "").strip()
        if not bvid:
            raise ValueError("视频帖子缺少 bvid (id)")
        exe = os.environ.get("BBDOWN_PATH") or shutil.which("BBDown")
        if not exe:
            raise ValueError(
                "未找到 BBDown, 请设置环境变量 BBDOWN_PATH 或在 PATH 中提供 BBDown"
            )
        video_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(
            (
                f for f in video_dir.iterdir()
                if f.is_file() and f.name.startswith(bvid)
                and f.suffix.lower() in VIDEO_EXTS
            ),
            key=lambda f: f.name,
        )
        if existing:
            first = existing[0]
            return {
                "skipped": True,
                "path": str(first),
                "stem": first.stem,
                "id": post.id,
                "files": [f.name for f in existing],
            }
        before = {f.name for f in video_dir.iterdir() if f.is_file()}
        cmd = [
            exe, bvid,
            "--work-dir", str(video_dir),
            "--skip-cover", "--skip-subtitle", "--skip-ai",
            "-F", f"{bvid}_<videoTitle>",
            "-M", f"{bvid}_<videoTitle>_P<pageNumberWithZero>_<pageTitle>",
        ]
        # 画质与编码优先级经环境变量配置, 非空时显式传给 BBDown;
        # 留空即采用 BBDown 默认策略 (清晰度最高优先)
        for env_key, flag in (
            ("BBDOWN_DFN_PRIORITY", "-q"),
            ("BBDOWN_ENCODING_PRIORITY", "-e"),
        ):
            value = os.environ.get(env_key, "").strip()
            if value:
                cmd += [flag, value]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        if emit:
            emit(0, 100)
        # 下载序号 / 总分P数: 由 BBDown 的 "开始解析P..: .. (n of m)" 行推进,
        # 单 P 视频无此行, 恒为 (1, 1)
        page_idx, page_total = 1, 1
        # 保留末尾若干行非进度日志, 供失败时定位原因 (BBDown 失败原因仅打印在控制台)
        tail: list[str] = []
        async for text in _iter_console_lines(process.stdout):
            m = re.search(r"开始解析P\d+: .*\((\d+) of (\d+)\)", text)
            if m:
                page_idx, page_total = int(m.group(1)), int(m.group(2))
                if emit:
                    # 分 P 开始时先推进阶梯进度 (管道模式下 BBDown 不输出
                    # 百分比进度条, 分 P 边界是唯一可靠进度信号)
                    emit(round((page_idx - 1) / page_total * 100), 100)
            pcts = re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", text)
            if pcts:
                if emit:
                    overall = (page_idx - 1 + float(pcts[-1]) / 100.0) / page_total
                    emit(min(100, round(overall * 100)), 100)
            elif not m:
                tail.append(text[-200:])
                tail = tail[-5:]
        await process.wait()
        if process.returncode != 0:
            raise RuntimeError(
                f"BBDown 下载失败, 退出码 {process.returncode}\n" + "\n".join(tail)
            )
        after = {f.name for f in video_dir.iterdir() if f.is_file()}
        created = sorted(after - before)
        if emit:
            emit(100, 100)
        if not created:
            raise ValueError("BBDown 未产出任何文件")
        return {
            "path": str(video_dir / created[0]),
            "stem": Path(created[0]).stem,
            "id": post.id,
            "files": created,
        }
