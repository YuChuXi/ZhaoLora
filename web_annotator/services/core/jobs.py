"""长任务编排: asyncio.Lock 串行, 整段 asyncio.to_thread, Server-Sent Events 推送进度.

Callers:
    - ``routes/meta.py:find_active_job``
    - ``routes/meta.py:job_events``
    - ``routes/meta.py:job_cancel``
    - ``routes/scoring.py:start`` (via ``submit``)
    - ``routes/tagging.py:start`` (via ``submit``)
    - ``routes/dedupe.py:scan_duplicates`` (via ``submit``)
    - ``routes/tag_stats.py:clean``
    - ``routes/tag_stats.py:batch_edit`` (via ``submit``)
    - ``routes/online_sources.py:batch_start``
    - ``routes/online_sources.py:refresh_metadata`` (via ``submit``)
    - ``routes/birefnet.py:mask_generate`` (via ``submit``)
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
    """单个长任务状态.

    Callers:
        - ``services/core/jobs.py:JobManager``
        - ``services/core/jobs.py:JobManager.event_stream`` (SSE 事件流)
    """

    id: str
    kind: str
    group: str
    dataset: str
    latest: JobSnapshot = field(default_factory=lambda: JobSnapshot(status="queued"))
    event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_fn: Callable[[], None] | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)


class JobManager:
    """全局长任务管理器.

    通过 ``group`` 参数实现互斥分组: 同一组内任务串行, 不同组之间可并行.

    Callers:
        - ``server.py:main`` (挂到 ``app.state.jobs``)
        - ``routes/meta.py:find_active_job`` / `job_events` / `job_cancel`
    """

    def __init__(self) -> None:
        """初始化锁与任务表.

        Returns:
            None

        Callers:
            - ``server.py:main``
        """
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        """按 id 取任务.

        Args:
            job_id: 任务 ID.

        Returns:
            Job | None: 任务对象; 不存在时为 None.

        Callers:
            - ``routes/meta.py:job_events``
        """
        return self._jobs.get(job_id)

    def find_active(
        self,
        *,
        dataset: str | None = None,
        kind: str | None = None,
    ) -> Job | None:
        """找最近一个未结束任务(刷新后重连).

        Args:
            dataset: 数据集名过滤; None 表示不限.
            kind: 任务类型过滤; None 表示不限.

        Returns:
            Job | None: 最近一个未结束任务; 无则 None.

        Callers:
            - ``routes/meta.py:find_active_job``
            - ``routes/online_sources.py:_submit_video_download`` / `batch_start` / `batch_params`
        """
        found: Job | None = None
        for job in self._jobs.values():
            if job.latest.status in TERMINAL:
                continue
            if dataset is not None and job.dataset != dataset:
                continue
            if kind is not None and job.kind != kind:
                continue
            found = job
        return found

    async def submit(
        self,
        *,
        kind: str,
        group: str,
        dataset: str,
        run: RunFn,
        cancel_fn: Callable[[], None] | None = None,
    ) -> str:
        """
        提交长任务, 立即返回 job_id.

        声明为协程: 内部用 ``loop.create_task`` 注册后台执行, 须在事件循环中
        调用; ``async`` 签名将此隐式契约显式化, 防止同步调用方在线程池中触发
        ``no running event loop``.

        Args:
            kind: 任务类型标识(aesthetic / tagging / dedupe_scan / tag_clean).
            dataset: 数据集名称.
            group: 互斥分组名, 同组内任务串行执行.
            run: 同步可调用对象, 签名 ``run(emit)``；``emit(JobSnapshot)`` 可从工作线程调用.
            cancel_fn: 协作取消回调.

        Returns:
            str: 任务 ID.

        Callers:
            - ``services/dataset/runtime.py:DatasetRegistry.ensure``
            - ``routes/scoring.py:start``
            - ``routes/tagging.py:start``
            - ``routes/dedupe.py:scan_duplicates``
            - ``routes/tag_stats.py:clean``
            - ``routes/tag_stats.py:batch_edit``
            - ``routes/online_sources.py:batch_start``
            - ``routes/online_sources.py:refresh_metadata``
            - ``routes/birefnet.py:mask_generate``
        """
        job_id = uuid.uuid4().hex
        job = Job(
            id=job_id,
            kind=kind,
            group=group,
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
        """请求协作取消.

        Args:
            job_id: 任务 ID.

        Returns:
            JobSnapshot: 标记为 ``cancel_requested`` 的最新快照.

        Raises:
            KeyError: 任务不存在.

        Callers:
            - ``routes/meta.py:job_cancel``
        """
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
        """写入最新快照并唤醒 SSE 等待方.

        Args:
            job: 任务对象.
            snap: 待发布的快照.

        Returns:
            None
        """
        Callers:
            - ``services/core/jobs.py:JobManager._execute``
            - ``services/core/jobs.py:JobManager.cancel``
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
        """工作线程安全地回传进度.

        Args:
            loop: 事件循环(用于线程安全回调).
            job: 任务对象.
            snap: 待发布的快照.

        Returns:
            None
        """
        Callers:
            - ``services/core/jobs.py:JobManager._execute``
            - ``services/core/jobs.py:JobManager.emit``
        loop.call_soon_threadsafe(self._publish, job, snap)

    async def _execute(self, job: Job, run: RunFn) -> None:
        """在锁内执行任务并发布终态.

        Args:
            job: 任务对象.
            run: 同步可调用对象, 签名 ``run(emit)``.

        Returns:
            None
        """
        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        loop = asyncio.get_running_loop()

        def emit(snap: JobSnapshot) -> None:
            """线程安全发布快照.

            Args:
                snap: 待发布的快照.

            Returns:
                None

            Callers:
                - ``services/core/jobs.py:JobManager._execute`` (作为 ``emit`` 回调传入 ``run``)
            """
            self._emit_from_thread(loop, job, snap)

        async with self._locks.setdefault(job.group, asyncio.Lock()):
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
                """在工作线程执行 ``run``.

                Returns:
                    JobResult | None: ``run`` 的返回值.

                Callers:
                    - ``services/core/jobs.py:JobManager._execute`` (经 ``asyncio.to_thread`` 调度)
                """
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
        """异步生成 Server-Sent Events 文本块.

        Args:
            job_id: 任务 ID.

        Returns:
            AsyncIterator[str]: SSE 文本块迭代器.

        Callers:
            - ``routes/meta.py:job_events``
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
    """序列化为 SSE ``data:`` 行.

    Args:
        data: 任务快照.

    Returns:
        str: SSE 文本块.
    """
    Callers:
        - ``services/core/jobs.py:JobManager.event_stream``
    return f"data: {data.model_dump_json()}\n\n"
