"""Booru 批量下载长任务（工作线程内 asyncio 跑搜索与下载）。

引用: ``routes.booru``；进度经 ``JobManager`` SSE 推送。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.booru_client import download_post, is_downloaded, local_rel_path, search_posts
from services.models import JobSnapshot

PAGE_LIMIT = 40
EMPTY_STREAK_MAX = 8
EmitFn = Callable[[JobSnapshot], None]


@dataclass
class BooruBatchProgress:
    """可变进度与运行参数。引用：``run_booru_batch``、参数热更新接口。"""

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
    kind: str = "booru_batch"
    dataset: str = ""

    def snapshot(self) -> JobSnapshot:
        """组装 JobSnapshot。引用：``emit``。"""
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


_ACTIVE: dict[str, BooruBatchProgress] = {}


def register_progress(job_id: str, prog: BooruBatchProgress) -> None:
    """登记进行中批量任务。引用：``routes.booru.booru_batch_start``。"""
    _ACTIVE[job_id] = prog


def unregister_progress(job_id: str) -> None:
    """移除登记。引用：``run_booru_batch`` finally。"""
    _ACTIVE.pop(job_id, None)


def get_progress(job_id: str) -> BooruBatchProgress | None:
    """按 job_id 取进度对象。引用：参数热更新。"""
    return _ACTIVE.get(job_id)


def find_progress_for_dataset(dataset: str) -> tuple[str, BooruBatchProgress] | None:
    """找该数据集上仍登记的批量进度。引用：active 接口。"""
    for jid, prog in _ACTIVE.items():
        if prog.dataset == dataset and prog.status not in ("done", "error", "cancelled"):
            return jid, prog
    return None


def run_booru_batch(
    *,
    job_id: str,
    source: str,
    tags: str,
    extra: str,
    order: str,
    out_dir: Path,
    root: Path,
    dir_name: str,
    start_page: int,
    proxy: str | None,
    progress: BooruBatchProgress,
    emit: EmitFn,
    thumbnailer: Any | None = None,
) -> None:
    """
    同步入口：在工作线程内 ``asyncio.run`` 执行批量下载。

    引用：``routes.booru.booru_batch_start``。
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
                dir_name=dir_name,
                start_page=start_page,
                proxy=proxy,
                progress=progress,
                emit=emit,
                thumbnailer=thumbnailer,
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
    dir_name: str,
    start_page: int,
    proxy: str | None,
    progress: BooruBatchProgress,
    emit: EmitFn,
    thumbnailer: Any | None,
) -> None:
    """异步批量：翻页搜索 + 并发下载。引用：``run_booru_batch``。"""

    def push(msg: str = "") -> None:
        """推送快照。引用：各阶段。"""
        if msg:
            progress.message = msg
        emit(progress.snapshot())

    progress.status = "running"
    progress.phase = "download"
    push("准备中…")

    page = max(1, int(start_page))
    empty_streak = 0
    exhausted = False
    buffer: list[Any] = []
    buf_i = 0
    inflight = 0
    gate = asyncio.Lock()
    rate_budget = 0.0
    rate_last = time.monotonic()
    rate_lock = asyncio.Lock()

    async def take_rate() -> None:
        """令牌桶限速。引用：``download_one``。"""
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

    async def fill_buffer() -> bool:
        """向后翻页填充候选。返回是否还有可能更多。引用：``next_post``。"""
        nonlocal page, empty_streak, exhausted, buffer, buf_i
        if exhausted:
            return False
        posts, has_more = await search_posts(
            source,
            tags,
            page=page,
            limit=PAGE_LIMIT,
            order=order,
            extra=extra,
            proxy=proxy,
        )
        page += 1
        if not posts:
            empty_streak += 1
            if not has_more or empty_streak >= EMPTY_STREAK_MAX:
                exhausted = True
                return False
            return True
        empty_streak = 0
        fresh = 0
        for p in posts:
            if is_downloaded(out_dir, source, p.id):
                progress.skipped += 1
                continue
            buffer.append(p)
            fresh += 1
        if fresh == 0 and not has_more:
            exhausted = True
        elif not has_more and fresh == 0:
            exhausted = True
        elif not has_more:
            # 本页有候选，但后面没有更多页；buffer 耗尽后结束
            pass
        if not has_more and fresh == 0 and not buffer[buf_i:]:
            exhausted = True
        if not has_more:
            # 标记：当前 buffer 用完且无更多页
            if fresh == 0:
                exhausted = True
            else:
                # 还有本页候选；设标志在 buffer 空时结束
                exhausted = False
                # 用空 streak 之外：记 no_more
                fill_buffer.no_more = True  # type: ignore[attr-defined]
        return True

    fill_buffer.no_more = False  # type: ignore[attr-defined]

    async def next_post() -> Any | None:
        """取下一个未下载帖。引用：``worker``。"""
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
                    if is_downloaded(out_dir, source, p.id):
                        progress.skipped += 1
                        push()
                        continue
                    inflight += 1
                    return p
                if exhausted or getattr(fill_buffer, "no_more", False):
                    if buf_i >= len(buffer):
                        exhausted = True
                        return None
                before = len(buffer)
                ok = await fill_buffer()
                push()
                if len(buffer) == before and (exhausted or not ok):
                    return None

    async def download_one(post: Any) -> None:
        """单帖下载（含 429 退避）。引用：``worker``。"""
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
                        post.id,
                        out_dir,
                        proxy=proxy,
                        overwrite=False,
                    )
                    if result.skipped:
                        progress.skipped += 1
                    else:
                        progress.done += 1
                        if thumbnailer is not None:
                            rel = local_rel_path(root, result.path)
                            if not rel.startswith(dir_name + "/") and "/" not in rel:
                                rel = f"{dir_name}/{Path(result.path).name}"
                            elif not rel.startswith(dir_name):
                                rel = local_rel_path(root, result.path)
                            thumbnailer.ensure(rel)
                    push()
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
        """并发槽位。引用：主循环。"""
        while not progress.cancel_requested:
            if slot >= progress.conn:
                await asyncio.sleep(0.1)
                continue
            if progress.done >= progress.limit:
                return
            post = await next_post()
            if post is None:
                if exhausted and inflight <= 0:
                    return
                await asyncio.sleep(0.12)
                if exhausted and inflight <= 0:
                    return
                continue
            await download_one(post)

    workers = [asyncio.create_task(worker(i)) for i in range(8)]
    await asyncio.gather(*workers)

    if progress.cancel_requested:
        progress.status = "cancelled"
        progress.message = "已取消"
    elif progress.failed and progress.done == 0 and exhausted:
        progress.status = "done"
        progress.message = "已完成（无新图或已耗尽）"
    else:
        progress.status = "done"
        progress.message = "已完成"
    push()
