"""
长任务编排：asyncio.Lock 串行，整段 asyncio.to_thread，Server-Sent Events 推送进度。

引用：routes/jobs.py；routes/aesthetic.py、tagging.py、dedupe.py、tag_stats.py 通过 submit 提交。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TERMINAL = frozenset({"done", "error", "cancelled"})


@dataclass
class Job:
    """单个长任务状态。引用：JobManager、SSE 事件流。"""

    id: str
    kind: str
    dataset: str
    latest: dict[str, Any] = field(default_factory=dict)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_fn: Callable[[], None] | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)


class JobManager:
    """
    全局长任务管理器。

    同一时刻仅一个任务持有锁执行；其余 Task 在锁上等待。
    引用：server.py 挂到 app.state.jobs；routes/jobs.py。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def submit(
        self,
        *,
        kind: str,
        dataset: str,
        run: Callable[[Callable[[dict[str, Any]], None]], Any],
        cancel_fn: Callable[[], None] | None = None,
    ) -> str:
        """
        提交长任务，立即返回 job_id。

        Args:
            kind: 任务类型标识（aesthetic / tagging / dedupe_scan / tag_clean）。
            dataset: 数据集名称。
            run: 同步可调用对象，签名 ``run(emit)``；``emit(dict)`` 可从工作线程调用。
            cancel_fn: 协作取消回调。
        """
        job_id = uuid.uuid4().hex
        job = Job(
            id=job_id,
            kind=kind,
            dataset=dataset,
            latest={"status": "queued", "kind": kind, "dataset": dataset, "job_id": job_id},
            cancel_fn=cancel_fn,
        )
        self._jobs[job_id] = job
        job.event.set()
        loop = asyncio.get_running_loop()
        job._task = loop.create_task(self._execute(job, run))
        return job_id

    def cancel(self, job_id: str) -> dict[str, Any]:
        """请求协作取消。引用：routes/jobs.py。"""
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        status = job.latest.get("status")
        if status in TERMINAL:
            return dict(job.latest)
        if job.cancel_fn is not None:
            job.cancel_fn()
        snap = dict(job.latest)
        snap["cancel_requested"] = True
        snap["message"] = snap.get("message") or "正在取消…"
        self._publish(job, snap)
        return snap

    def _publish(self, job: Job, snap: dict[str, Any]) -> None:
        payload = dict(snap)
        payload.setdefault("job_id", job.id)
        payload.setdefault("kind", job.kind)
        payload.setdefault("dataset", job.dataset)
        job.latest = payload
        job.event.set()

    def _emit_from_thread(
        self,
        loop: asyncio.AbstractEventLoop,
        job: Job,
        snap: dict[str, Any],
    ) -> None:
        loop.call_soon_threadsafe(self._publish, job, snap)

    async def _execute(
        self,
        job: Job,
        run: Callable[[Callable[[dict[str, Any]], None]], Any],
    ) -> None:
        loop = asyncio.get_running_loop()

        def emit(snap: dict[str, Any]) -> None:
            self._emit_from_thread(loop, job, snap)

        async with self._lock:
            if job.latest.get("cancel_requested") and job.latest.get("status") == "queued":
                self._publish(job, {
                    "status": "cancelled",
                    "message": "已在等待执行时取消",
                    "kind": job.kind,
                    "dataset": job.dataset,
                })
                return

            running = dict(job.latest)
            running["status"] = "running"
            self._publish(job, running)

            def thread_main() -> Any:
                return run(emit)

            try:
                result = await asyncio.to_thread(thread_main)
            except Exception as e:
                self._publish(job, {
                    "status": "error",
                    "message": str(e),
                    "kind": job.kind,
                    "dataset": job.dataset,
                })
                return

            final = dict(job.latest)
            if final.get("status") not in TERMINAL:
                final["status"] = "done"
                if result is not None:
                    final["result"] = result
                self._publish(job, final)

    async def event_stream(self, job_id: str):
        """
        异步生成 Server-Sent Events 文本块。

        引用：routes/jobs.py。
        """
        job = self._jobs.get(job_id)
        if job is None:
            yield _sse({"status": "error", "message": "任务不存在", "job_id": job_id})
            return

        last_sent: dict[str, Any] | None = None
        while True:
            snap = dict(job.latest)
            if snap != last_sent:
                yield _sse(snap)
                last_sent = snap
                if snap.get("status") in TERMINAL:
                    return
            job.event.clear()
            if job.latest != last_sent:
                continue
            await job.event.wait()


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
