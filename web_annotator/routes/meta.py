"""任务列表别名与长任务 SSE / 取消。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from routes.deps import jobs
from services.models import DatasetItem, DatasetsResponse, JobCancelResponse

datasets_router = APIRouter(tags=["datasets"])
jobs_router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@datasets_router.get("/api/datasets")
def list_datasets(request: Request) -> DatasetsResponse:
    """
    返回清单中未完成的任务名，供业务页路径校验。

    引用：nav.js boot。
    """
    store = request.app.state.task_store
    items = [
        DatasetItem(name=name, root=str(store.root / name))
        for name in store.active_names()
    ]
    return DatasetsResponse(datasets=items)


@jobs_router.get("/{job_id}/events")
async def job_events(request: Request, job_id: str) -> StreamingResponse:
    """
    Server-Sent Events：推送任务进度直至结束。

    引用：前端 API.watchJob。
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
    请求协作取消长任务。

    引用：aesthetic.js、tagging.js。
    """
    mgr = jobs(request)
    try:
        snap = mgr.cancel(job_id)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None
    return JobCancelResponse(ok=True, status=snap)
