"""任务列表别名与长任务 SSE / 取消 / 活跃查询. """

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from routes.deps import jobs
from services.models import DatasetItem, DatasetsResponse, JobCancelResponse, JobSnapshot

datasets_router = APIRouter(tags=["datasets"])
jobs_router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@datasets_router.get("/api/datasets")
def list_datasets(request: Request) -> DatasetsResponse:
    """
    返回清单中未完成的任务名, 供业务页路径校验. 

    引用: nav.js boot. 
    """
    store = request.app.state.task_store
    items = [DatasetItem(name=name) for name in store.active_names()]
    return DatasetsResponse(datasets=items)


@jobs_router.get("/active")
def find_active_job(
    request: Request,
    kind: str = Query(..., description="任务类型: archive / aesthetic / quality / online_batch 等"),
    dataset: str | None = Query(None, description="可选, 按数据集名过滤"),
) -> JobSnapshot:
    """查询指定类型的活跃长任务(未结束).

    页面刷新时调用此端点获取 job_id, 再通过 SSE 重连恢复进度.

    Args:
        kind: 任务类型标识.
        dataset: 数据集名, 省略则不限.

    Returns:
        JobSnapshot: 含 job_id; 无活跃任务时 status="idle".

    Callers:
        - ``api.js`` : ``findActiveJob`` 通用重连.
    """
    mgr = jobs(request)
    job = mgr.find_active(kind=kind, dataset=dataset)
    if job is None:
        return JobSnapshot(status="idle")
    snap = job.latest
    return JobSnapshot(
        status=snap.status,
        kind=job.kind,
        dataset=job.dataset,
        job_id=job.id,
        message=snap.message,
        done=snap.done,
        total=snap.total,
        pct=snap.pct,
        phase=snap.phase,
        current=snap.current,
    )


@jobs_router.get("/{job_id}/events")
async def job_events(request: Request, job_id: str) -> StreamingResponse:
    """
    Server-Sent Events: 推送任务进度直至结束. 

    引用: 前端 API.watchJob. 
    """
    mgr = jobs(request)
    if mgr.get(job_id) is None:
        raise HTTPException(404, "任务不存在")

    return StreamingResponse(
        mgr.event_stream(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@jobs_router.post("/{job_id}/cancel")
def job_cancel(request: Request, job_id: str) -> JobCancelResponse:
    """
    请求协作取消长任务. 

    引用: aesthetic.js、tagging.js. 
    """
    mgr = jobs(request)
    try:
        snap = mgr.cancel(job_id)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None
    return JobCancelResponse(ok=True, status=snap)
