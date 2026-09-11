"""在线来源批量下载长任务(工作线程内 asyncio 跑搜索与下载).

进度经 ``JobManager`` SSE 推送.

Callers:
    - ``routes/online_sources.py:batch_start``
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.online_sources.framework.base import get_source
from services.online_sources.framework.media import download_post, local_rel_path
from services.online_sources.framework.utils import is_downloaded
from services.dataset.layout import VIDEO_DIRNAME
from services.models import JobSnapshot

# 连续空页判定抓完的阈值: 会话式来源(如 B 站)的风控空心响应与真实末页结构
# 一致, 需连续多次空页(退避重试同一游标)才能确认抓完, 避免批量提前停止
EMPTY_STREAK_MAX = 6
# 空页退避基数 (秒), 指数递增并封顶, 用于会话式来源空页后的重试等待
_EMPTY_BACKOFF_BASE = 1.5
_EMPTY_BACKOFF_CAP = 15.0
EmitFn = Callable[[JobSnapshot], None]


def _empty_backoff(streak: int) -> float:
    """计算第 streak 次空页后的重试退避秒数(指数递增, 封顶).

    Args:
        streak (int): 连续空页次数 (从 1 起)。

    Returns:
        float: 退避秒数。

    Callers:
        - ``services/online_sources/batch.py:fill_buffer``
    """
    return min(_EMPTY_BACKOFF_BASE * (2 ** (streak - 1)), _EMPTY_BACKOFF_CAP)


@dataclass
class BatchProgress:
    """可变进度与运行参数.

    Callers:
        - ``services/online_sources/batch.py:run_batch``
        - ``routes/online_sources.py:batch_params``
    """

    cancel_requested: bool = False
    limit: int = 50
    conn: int = 2
    rate_rps: float = 0.0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    status: str = "running"
    message: str = ""
    phase: str = "download"
    kind: str = "online_batch"
    dataset: str = ""

    def snapshot(self) -> JobSnapshot:
        """组装 JobSnapshot.

        Returns:
            JobSnapshot: 当前进度的快照, 含百分比、计数与状态消息.

        Callers:
            - ``services/online_sources/batch.py:_run_async``
        """
        total = max(0, int(self.limit))
        done = max(0, int(self.done))
        pct = min(100.0, (done / total) * 100.0) if total > 0 else None
        bits: list[str] = []
        if self.skipped > 0:
            bits.append(f"跳过重复 {self.skipped}")
        if self.failed > 0:
            bits.append(f"失败 {self.failed}")
        if self.message:
            bits.append(self.message)
        return JobSnapshot(
            status=self.status,
            kind=self.kind,
            dataset=self.dataset,
            done=done,
            total=total,
            ok=done,
            skipped=self.skipped,
            failed=self.failed,
            pct=pct,
            phase=self.phase,
            message=" · ".join(bits),
            cancel_requested=self.cancel_requested,
        )


_ACTIVE: dict[str, BatchProgress] = {}


def register_progress(job_id: str, prog: BatchProgress) -> None:
    """登记进行中的批量下载任务, 供热更新接口查询.

    Args:
        job_id (str): 任务 ID.
        prog (BatchProgress): 进度对象.

    Returns:
        None: 无返回值.

    Callers:
        - ``routes/online_sources.py:batch_start``
        - ``services/online_sources/batch.py:run_batch``
    """
    _ACTIVE[job_id] = prog


def unregister_progress(job_id: str) -> None:
    """移除已结束批量任务的登记.

    Args:
        job_id (str): 任务 ID.

    Returns:
        None: 无返回值.

    Callers:
        - ``services/online_sources/batch.py:run_batch``
    """
    _ACTIVE.pop(job_id, None)


def get_progress(job_id: str) -> BatchProgress | None:
    """按 job_id 获取进度对象.

    Args:
        job_id (str): 任务 ID.

    Returns:
        BatchProgress | None: 进度对象; 不存在时返回 None.

    Callers:
        - ``routes/online_sources.py:batch_params``
    """
    return _ACTIVE.get(job_id)


def run_batch(
    *,
    job_id: str,
    source: str,
    tags: str,
    extra: str,
    order: str,
    out_dir: Path,
    root: Path,
    start_page: int,
    progress: BatchProgress,
    emit: EmitFn,
    thumbnailer: Any | None = None,
    anima_format: bool = True,
    include_meta_tags: bool = False,
    keep_underscores: bool = False,
) -> None:
    """同步入口: 在工作线程内 ``asyncio.run`` 执行批量下载.

    Args:
        job_id (str): 任务 ID.
        source (str): 来源名.
        tags (str): 搜索标签表达式.
        extra (str): 附加查询参数.
        order (str): 排序方式.
        out_dir (Path): 输出目录.
        root (Path): 数据集根目录, 用于计算相对路径.
        start_page (int): 起始页码.
        progress (BatchProgress): 可变进度对象.
        emit (EmitFn): 快照推送回调.
        thumbnailer (Any | None): 缩略图生成器, 可选.
        anima_format (bool): 是否按 Anima 格式输出标签.
        include_meta_tags (bool): 是否保留元数据标签.
        keep_underscores (bool): 是否保留下划线.

    Returns:
        None: 无返回值, 进度经 ``emit`` 推送.

    Callers:
        - ``routes/online_sources.py:batch_start``
    """
    register_progress(job_id, progress)
    try:
        asyncio.run(
            _run_async(
                source=source,
                tags=tags,
                extra=extra,
                order=order,
                out_dir=out_dir,
                root=root,
                start_page=start_page,
                progress=progress,
                emit=emit,
                thumbnailer=thumbnailer,
                anima_format=anima_format,
                include_meta_tags=include_meta_tags,
                keep_underscores=keep_underscores,
            )
        )
    finally:
        unregister_progress(job_id)


async def _run_async(
    *,
    source: str,
    tags: str,
    extra: str,
    order: str,
    out_dir: Path,
    root: Path,
    start_page: int,
    progress: BatchProgress,
    emit: EmitFn,
    thumbnailer: Any | None,
    anima_format: bool = True,
    include_meta_tags: bool = False,
    keep_underscores: bool = False,
) -> None:
    """异步批量下载核心: 翻页搜索 + 并发下载, 含令牌桶限速与 429 退避.

    Args:
        source (str): 来源名.
        tags (str): 搜索标签表达式.
        extra (str): 附加查询参数.
        order (str): 排序方式.
        out_dir (Path): 输出目录.
        root (Path): 数据集根目录, 用于计算相对路径.
        start_page (int): 起始页码.
        progress (BatchProgress): 可变进度对象.
        emit (EmitFn): 快照推送回调.
        thumbnailer (Any | None): 缩略图生成器, 可选.
        anima_format (bool): 是否按 Anima 格式输出标签.
        include_meta_tags (bool): 是否保留元数据标签.
        keep_underscores (bool): 是否保留下划线.

    Returns:
        None: 无返回值, 进度经 ``emit`` 推送.

    Callers:
        - ``services/online_sources/batch.py:run_batch``
    """

    def push(msg: str | None = None) -> None:
        """推送快照.

        Args:
            msg (str | None): 可选状态消息, 写入 ``progress.message`` 后推送.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/batch.py:_run_async``
        """
        if msg is not None:
            progress.message = msg
        emit(progress.snapshot())

    progress.status = "running"
    progress.phase = "download"
    push("准备中…")

    page = max(1, int(start_page))
    session_token = ""  # 会话式来源的续拉 token; 首轮空表示从头拉取
    empty_streak = 0
    has_more_pages = True
    buffer: list[Any] = []
    buf_i = 0
    inflight = 0
    gate = asyncio.Lock()
    rate_budget = 0.0
    rate_last = time.monotonic()
    rate_lock = asyncio.Lock()

    async def take_rate() -> None:
        """令牌桶限速.

        Returns:
            None: 无返回值; 阻塞至令牌可用或任务被取消.

        Callers:
            - ``services/online_sources/batch.py:download_one``
        """
        nonlocal rate_budget, rate_last
        rps = float(progress.rate_rps)
        if rps <= 0:
            return
        async with rate_lock:
            while True:
                if progress.cancel_requested:
                    return
                now = time.monotonic()
                rate_budget += (now - rate_last) * rps
                rate_last = now
                if rate_budget > rps * 2:
                    rate_budget = rps * 2
                if rate_budget >= 1.0:
                    rate_budget -= 1.0
                    return
                wait = (1.0 - rate_budget) / rps
                rate_budget = 0.0
                await asyncio.sleep(min(120.0, max(0.01, wait)))

    async def fill_buffer() -> None:
        """向后翻一页并加入未下载候选.

        视频帖不做工作目录文件判定 (其产物在数据集 ``video`` 目录, 由
        BBDown 以 BV 前缀判定, 见 ``download_one``), 一律进入候选。

        Returns:
            None: 无返回值; 候选写入闭包 ``buffer`` 并更新 ``has_more_pages``.

        Callers:
            - ``services/online_sources/batch.py:next_post``
        """
        nonlocal page, session_token, empty_streak, has_more_pages
        if not has_more_pages:
            return
        src = get_source(source)
        if src.session_based:
            # 会话式来源: 从已持有会话续拉; 首轮 token 为空即从头拉取
            posts, has_more, session_token = await src.search_session(
                tags, session_token, order, extra=extra,
            )
        else:
            posts, has_more = await src.search(
                tags, page, order, extra=extra,
            )
            page += 1
        for p in posts:
            p.stem = src.file_stem(p.id)
            p.filename = f"{p.stem}.png"
        if posts:
            empty_streak = 0
            if not has_more:
                # 拿到末页(记录存在且无后续游标), 判定抓完
                has_more_pages = False
            for p in posts:
                if not p.is_video and is_downloaded(out_dir, p.filename):
                    progress.skipped += 1
                else:
                    buffer.append(p)
            return
        # 空页: 页码式来源 has_more=False 即为末页; 会话式来源的空响应可能是
        # 风控(与末页结构一致), 需连续多次空页(退避重试同一游标)才判定抓完
        empty_streak += 1
        if (not has_more and not src.session_based) or empty_streak >= EMPTY_STREAK_MAX:
            has_more_pages = False
        elif src.session_based:
            await asyncio.sleep(_empty_backoff(empty_streak))

    async def next_post() -> Any | None:
        """取下一个未下载帖.

        Returns:
            Any | None: 下一个待下载帖; 达到上限、翻完或被取消时返回 None.

        Callers:
            - ``services/online_sources/batch.py:worker``
        """
        nonlocal buf_i, inflight
        async with gate:
            while True:
                if progress.cancel_requested:
                    return None
                if progress.done + inflight >= progress.limit:
                    return None
                while buf_i < len(buffer):
                    p = buffer[buf_i]
                    buf_i += 1
                    if not p.is_video and is_downloaded(out_dir, p.filename):
                        progress.skipped += 1
                        push("")
                        continue
                    inflight += 1
                    return p
                if not has_more_pages:
                    return None
                await fill_buffer()
                push("")

    async def download_one(post: Any) -> None:
        """单帖下载(含 429 退避).

        视频帖的目标目录为数据集根 ``video`` (BBDown 产物原位写入),
        其余帖子落工作目录; 视频文件不做缩略图。

        Args:
            post (Any): 待下载帖对象.

        Returns:
            None: 无返回值; 结果累计进 ``progress``.

        Callers:
            - ``services/online_sources/batch.py:worker``
        """
        nonlocal inflight
        attempt = 0
        try:
            while True:
                if progress.cancel_requested:
                    return
                await take_rate()
                if progress.cancel_requested:
                    return
                try:
                    result = await download_post(
                        source,
                        post,
                        root / VIDEO_DIRNAME if post.is_video else out_dir,
                        overwrite=False,
                        anima_format=anima_format,
                        include_meta_tags=include_meta_tags,
                        keep_underscores=keep_underscores,
                    )
                    if result.skipped:
                        progress.skipped += 1
                    else:
                        progress.done += 1
                        if thumbnailer is not None and not post.is_video:
                            rel = local_rel_path(root, result.path)
                            await thumbnailer.ensure_async(rel)
                    push("")
                    return
                except RuntimeError as e:
                    msg = str(e)
                    if "429" in msg and attempt < 10:
                        wait_sec = min(120, 2 ** (attempt + 1))
                        push(f"429 退避 {wait_sec}s")
                        await asyncio.sleep(wait_sec)
                        attempt += 1
                        continue
                    raise
        except Exception as e:
            if progress.cancel_requested:
                return
            progress.failed += 1
            push(f"失败 {getattr(post, 'id', '?')}: {e}")
        finally:
            inflight -= 1

    async def worker(slot: int) -> None:
        """并发槽位.

        Args:
            slot (int): 槽位编号, 与 ``progress.conn`` 比较决定是否活跃.

        Returns:
            None: 无返回值; 达到上限或翻完时退出.

        Callers:
            - ``services/online_sources/batch.py:_run_async``
        """
        while not progress.cancel_requested:
            if slot >= progress.conn:
                # 空闲槽位: 无并发额度时挂起等待, 但批量结束(达限或翻完且无
                # 在途下载)时一并退出, 避免 gather 因空转槽位永不返回
                if progress.done >= progress.limit or (not has_more_pages and inflight <= 0):
                    return
                await asyncio.sleep(0.1)
                continue
            if progress.done >= progress.limit:
                return
            post = await next_post()
            if post is None:
                if progress.cancel_requested:
                    return
                if progress.done >= progress.limit:
                    return
                if not has_more_pages and inflight <= 0:
                    return
                await asyncio.sleep(0.12)
                continue
            await download_one(post)

    workers = [asyncio.create_task(worker(i)) for i in range(8)]
    await asyncio.gather(*workers)

    if progress.cancel_requested:
        progress.status = "cancelled"
        progress.message = "已取消"
    else:
        progress.status = "done"
        progress.message = "已完成"
    push()
