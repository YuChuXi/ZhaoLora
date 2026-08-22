"""WD14 批量打标 API(数据集作用域). """

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs
from services.core.jobs import EmitFn
from services.models import TaggingDirsResponse, TaggingStartResponse
from services.analysis.wd14 import (
    DEFAULT_THRESHOLD,
    TaggingProgress,
    list_image_subdirs,
    model_ready,
    resolve_image_dirs,
    run_batch,
)

router = APIRouter(prefix="/tagging", tags=["tagging"])


class StartBody(BaseModel):
    """打标启动请求体. """

    dirs: list[str] = Field(default_factory=list, description="子目录名")
    threshold: float = Field(default=DEFAULT_THRESHOLD, ge=0.0, le=1.0)
    disable_orig_tags: bool = True
    force: bool = False
    artist_tag: str = Field(default="", description="画师标签, 需带 @ 前缀")


@router.get("/dirs")
def dirs(request: Request) -> TaggingDirsResponse:
    """列出可打标目录与 WD14 模型就绪状态.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.

    Returns:
        TaggingDirsResponse: 含子目录列表/模型就绪状态/模型路径/默认阈值.
    """
    rt = get_runtime(request)
    root = rt.root
    ok, model_path = model_ready()
    return TaggingDirsResponse(
        dirs=list_image_subdirs(root),
        model_ready=ok,
        model_path=model_path,
        default_threshold=DEFAULT_THRESHOLD,
    )


@router.post("/start")
async def start(request: Request, body: StartBody) -> TaggingStartResponse:
    """提交 WD14 批量打标长任务, 返回 job_id.

    进度经 SSE 推送, 见 ``GET /api/jobs/{job_id}/events``.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (StartBody): 启动参数, 含目录/阈值/是否禁用原 tag/是否强制重打/画师标签.

    Returns:
        TaggingStartResponse: 含 ``job_id`` 与本次启动的目录/阈值/选项回显.

    Raises:
        HTTPException: 模型未就绪或未选择目录时返回 400.

    Callers:
        - ``static/js/tagging.js:startJob``
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
        """工作线程入口: 调用 ``run_batch`` 执行批量打标.

        Args:
            emit (EmitFn): 任务进度发射回调, 由 ``JobManager`` 注入.

        Callers:
            - ``routes.tagging.start`` (作为 ``run`` 参数传入 ``services.core.jobs.JobManager.submit``)
        """
        def on_update() -> None:
            """推送打标进度快照到 SSE 通道."""
            emit(prog.snapshot())

        run_batch(
            selected,
            threshold=body.threshold,
            disable_orig_tags=body.disable_orig_tags,
            force=body.force,
            progress=prog,
            on_update=on_update,
            artist_tag=body.artist_tag.strip(),
        )
        on_update()

    job_id = await jobs(request).submit(
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