"""数据集列表与长任务 SSE / 取消。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from routes.deps import jobs, registry

datasets_router = APIRouter(tags=["datasets"])
jobs_router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@datasets_router.get("/api/datasets")
def list_datasets(request: Request):
    """
    列出已配置数据集。

    引用：首页、nav.js / api.js 启动校验。
    """
    reg = registry(request)
    items = []
    for name in reg.names():
        rt = reg.get(name)
        items.append({
            "name": name,
            "root": str(rt.root),
        })
    return {"datasets": items}


@jobs_router.get("/{job_id}/events")
async def job_events(request: Request, job_id: str):
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
def job_cancel(request: Request, job_id: str):
    """
    请求协作取消长任务。

    引用：aesthetic.js、tagging.js。
    """
    mgr = jobs(request)
    try:
        snap = mgr.cancel(job_id)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None
    return {"ok": True, "status": snap}
