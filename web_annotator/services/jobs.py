"""
长任务编排：asyncio.Lock 串行，整段 asyncio.to_thread，Server-Sent Events 推送进度。

引用：routes/meta.py；routes/aesthetic.py、tagging.py、dedupe.py、tag_stats.py 通过 submit 提交。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from services.models import JobResult, JobSnapshot

TERMINAL = frozenset({"done", "error", "cancelled"})

EmitFn = Callable[[JobSnapshot], None]
RunFn = Callable[[EmitFn], JobResult | None]


@dataclass
class Job:
    """单个长任务状态。

    引用:
        ``JobManager``、SSE 事件流。
    """

    id: str
    kind: str
    dataset: str
    latest: JobSnapshot = field(default_factory=lambda: JobSnapshot(status="queued"))
    event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_fn: Callable[[], None] | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)


class JobManager:
    """
    全局长任务管理器。

    同一时刻仅一个任务持有锁执行；其余 Task 在锁上等待。
    引用：server.py 挂到 app.state.jobs；routes/meta.py。
    """

    def __init__(self) -> None:
        """初始化锁与任务表。引用：``server.main``。"""
        self._lock = asyncio.Lock()
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        """按 id 取任务。引用：``routes.meta``。"""
        return self._jobs.get(job_id)

    def submit(
        self,
        *,
        kind: str,
        dataset: str,
        run: RunFn,
        cancel_fn: Callable[[], None] | None = None,
    ) -> str:
        """
        提交长任务，立即返回 job_id。

        Args:
            kind: 任务类型标识（aesthetic / tagging / dedupe_scan / tag_clean）。
            dataset: 数据集名称。
            run: 同步可调用对象，签名 ``run(emit)``；``emit(JobSnapshot)`` 可从工作线程调用。
            cancel_fn: 协作取消回调。

        引用:
            ``routes.aesthetic`` / ``tagging`` / ``dedupe`` / ``tag_stats``。
        """
        job_id = uuid.uuid4().hex
        job = Job(
            id=job_id,
            kind=kind,
            dataset=dataset,
            latest=JobSnapshot(
                status="queued",
                kind=kind,
                dataset=dataset,
                job_id=job_id,
            ),
            cancel_fn=cancel_fn,
        )
        self._jobs[job_id] = job
        job.event.set()
        loop = asyncio.get_running_loop()
        job._task = loop.create_task(self._execute(job, run))
        return job_id

    def cancel(self, job_id: str) -> JobSnapshot:
        """请求协作取消。引用：``routes.meta.job_cancel``。"""
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.latest.status in TERMINAL:
            return job.latest
        if job.cancel_fn is not None:
            job.cancel_fn()
        snap = job.latest.model_copy(
            update={
                "cancel_requested": True,
                "message": job.latest.message or "正在取消…",
            }
        )
        self._publish(job, snap)
        return snap

    def _publish(self, job: Job, snap: JobSnapshot) -> None:
        """写入最新快照并唤醒 SSE 等待方。引用：``cancel`` / ``_execute`` / ``emit``。"""
        payload = snap.model_copy(
            update={
                "job_id": snap.job_id or job.id,
                "kind": snap.kind or job.kind,
                "dataset": snap.dataset or job.dataset,
            }
        )
        job.latest = payload
        job.event.set()

    def _emit_from_thread(
        self,
        loop: asyncio.AbstractEventLoop,
        job: Job,
        snap: JobSnapshot,
    ) -> None:
        """工作线程安全地回传进度。引用：``_execute`` 内 ``emit``。"""
        loop.call_soon_threadsafe(self._publish, job, snap)

    async def _execute(self, job: Job, run: RunFn) -> None:
        """在锁内执行任务并发布终态。引用：``submit``。"""
        loop = asyncio.get_running_loop()

        def emit(snap: JobSnapshot) -> None:
            """线程安全发布快照。引用：``run``。"""
            self._emit_from_thread(loop, job, snap)

        async with self._lock:
            if job.latest.cancel_requested and job.latest.status == "queued":
                self._publish(
                    job,
                    JobSnapshot(
                        status="cancelled",
                        message="已在等待执行时取消",
                        kind=job.kind,
                        dataset=job.dataset,
                    ),
                )
                return

            self._publish(job, job.latest.model_copy(update={"status": "running"}))

            def thread_main() -> JobResult | None:
                """在工作线程执行 ``run``。引用：``asyncio.to_thread``。"""
                return run(emit)

            try:
                result = await asyncio.to_thread(thread_main)
            except Exception as e:
                self._publish(
                    job,
                    JobSnapshot(
                        status="error",
                        message=str(e),
                        kind=job.kind,
                        dataset=job.dataset,
                    ),
                )
                return

            final = job.latest
            if final.status not in TERMINAL:
                self._publish(
                    job,
                    final.model_copy(
                        update={
                            "status": "done",
                            "result": result,
                        }
                    ),
                )

    async def event_stream(self, job_id: str) -> AsyncIterator[str]:
        """
        异步生成 Server-Sent Events 文本块。

        Yields:
            SSE 文本块。

        引用：``routes.meta.job_events``。
        """
        job = self._jobs.get(job_id)
        if job is None:
            yield _sse(
                JobSnapshot(status="error", message="任务不存在", job_id=job_id)
            )
            return

        last_sent: JobSnapshot | None = None
        while True:
            snap = job.latest
            if snap != last_sent:
                yield _sse(snap)
                last_sent = snap
                if snap.status in TERMINAL:
                    return
            job.event.clear()
            if job.latest != last_sent:
                continue
            await job.event.wait()


def _sse(data: JobSnapshot) -> str:
    """序列化为 SSE ``data:`` 行。引用：``event_stream``。"""
    return f"data: {data.model_dump_json()}\n\n"
