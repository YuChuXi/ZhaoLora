"""WD14 批量打标 API（数据集作用域）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs
from services.jobs import EmitFn
from services.models import TaggingDirsResponse, TaggingStartResponse
from services.wd14_batch import (
    DEFAULT_THRESHOLD,
    TaggingProgress,
    list_image_subdirs,
    model_ready,
    resolve_image_dirs,
    run_batch,
)

router = APIRouter(prefix="/tagging", tags=["tagging"])


class StartBody(BaseModel):
    """打标启动请求。引用：``POST /tagging/start``。"""

    dirs: list[str] = Field(default_factory=list, description="子目录名")
    threshold: float = Field(default=DEFAULT_THRESHOLD, ge=0.0, le=1.0)
    disable_orig_tags: bool = True
    force: bool = True


@router.get("/dirs")
def tagging_dirs(request: Request) -> TaggingDirsResponse:
    """列出可打标目录与模型状态。引用：``tagging.js``。"""
    rt = get_runtime(request)
    root = rt.root
    ok, model_path = model_ready()
    return TaggingDirsResponse(
        root=str(root),
        dirs=list_image_subdirs(root),
        model_ready=ok,
        model_path=model_path,
        default_threshold=DEFAULT_THRESHOLD,
    )


@router.post("/start")
async def tagging_start(request: Request, body: StartBody) -> TaggingStartResponse:
    """
    提交打标长任务。

    返回 job_id；进度见 GET /api/jobs/{id}/events。
    引用：tagging.js。
    """
    rt = get_runtime(request)
    root = rt.root
    ok, msg = model_ready()
    if not ok:
        raise HTTPException(400, msg)

    selected = resolve_image_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    prog = TaggingProgress()

    def run(emit: EmitFn) -> None:
        """工作线程入口。引用：``JobManager.submit``。"""
        def on_update() -> None:
            """推送打标进度。引用：``run_batch``。"""
            emit(prog.snapshot())

        run_batch(
            selected,
            threshold=body.threshold,
            disable_orig_tags=body.disable_orig_tags,
            force=body.force,
            progress=prog,
            on_update=on_update,
        )
        on_update()

    job_id = jobs(request).submit(
        kind="tagging",
        group="compute",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    return TaggingStartResponse(
        ok=True,
        job_id=job_id,
        dirs=[p.name if p != root else "." for p in selected],
        threshold=body.threshold,
        disable_orig_tags=body.disable_orig_tags,
        force=body.force,
    )