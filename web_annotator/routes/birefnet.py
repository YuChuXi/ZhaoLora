"""BiRefNet 蒙版生成 API 路由. """

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs, resolve_path
from services.core.jobs import EmitFn
from services.core.scanner import IMAGE_EXTS
from services.analysis.birefnet import (
    BirefnetProgress,
    collect_images,
    run_birefnet_mask,
)

router = APIRouter(tags=["birefnet"])


class BirefnetStartBody(BaseModel):
    """BiRefNet 蒙版生成请求体. """

    dir: str = Field("main", description="目标子目录名, 默认 main")
    path: str | None = Field(None, description="单图模式: 图片相对数据集根路径(含子目录名), 仅处理该图; 缺省处理整个目录")
    mask_expand: int = Field(32, ge=0, le=200, description="蒙版向外扩展的像素数")
    mask_shrink: int = Field(0, ge=0, le=200, description="蒙版向内收缩的像素数")
    mask_min: int = Field(96, ge=0, le=255, description="蒙版值域下限 (0-255)")
    mask_max: int = Field(255, ge=0, le=255, description="蒙版值域上限 (0-255)")
    skip_existing: bool = Field(True, description="跳过已有蒙版文件")


class BirefnetStartResponse(BaseModel):
    """BiRefNet 任务启动响应. """

    ok: bool = True
    job_id: str


@router.post("/birefnet-mask")
async def birefnet_mask(request: Request, body: BirefnetStartBody) -> BirefnetStartResponse:
    """启动 BiRefNet 蒙版生成任务. 

    Callers:
        - `static/js/api.js`
    """
    rt = get_runtime(request)
    if body.path:
        src = resolve_path(rt.root, body.path)
        if not src.is_file() or src.suffix.lower() not in IMAGE_EXTS:
            raise HTTPException(400, f"无效图片路径: {body.path}")
        image_rel_paths = [body.path]
    else:
        image_rel_paths = collect_images(rt.root, subdir=body.dir)
    if not image_rel_paths:
        raise HTTPException(400, "没有可处理的图片")

    prog = BirefnetProgress()

    def run(emit: EmitFn) -> None:
        """工作线程入口. 

        Callers:
            - `JobManager.submit`
        """
        def on_progress() -> None:
            emit(prog.snapshot())

        run_birefnet_mask(
            image_rel_paths=image_rel_paths,
            data_dir=rt.root,
            mask_dir=rt.mask_dir,
            mask_expand=body.mask_expand,
            mask_shrink=body.mask_shrink,
            mask_min=body.mask_min,
            mask_max=body.mask_max,
            skip_existing=body.skip_existing,
            progress=prog,
            on_progress=on_progress,
        )
        snap = prog.snapshot()
        snap.status = "done"
        emit(snap)

    job_id = jobs(request).submit(
        kind="birefnet_mask",
        group="compute",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    return BirefnetStartResponse(job_id=job_id)
