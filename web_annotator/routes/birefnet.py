"""蒙版生成 API 路由 (BiRefNet 背景移除 / SAM3 概念分割). """

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
from services.analysis.mask_expression import ExpressionError, extract_concepts, parse
from services.analysis.sam3 import (
    Sam3MaskProgress,
    run_sam3_expression_mask,
)

router = APIRouter(tags=["birefnet"])

MASK_MODES = ("birefnet", "sam3")


class MaskGenerateBody(BaseModel):
    """蒙版生成请求体(按 ``mode`` 选择生成方式). """

    dir: str = Field("main", description="批量模式: 目标子目录名, 默认 main")
    path: str | None = Field(None, description="单图模式: 图片相对数据集根路径(含子目录名), 仅处理该图; 缺省处理整个目录")
    mode: str = Field("birefnet", description="生成方式: birefnet | sam3")
    expression: str | None = Field(None, description="sam3 模式的逻辑表达式, 如 (默认蒙板-(\"白色背景\"+\"对话气泡\"))")
    mask_expand: int = Field(32, ge=0, le=200, description="BiRefNet: 蒙版向外扩展的像素数")
    mask_shrink: int = Field(0, ge=0, le=200, description="BiRefNet: 蒙版向内收缩的像素数")
    mask_min: int = Field(96, ge=0, le=255, description="BiRefNet: 蒙版值域下限 (0-255)")
    mask_max: int = Field(255, ge=0, le=255, description="BiRefNet: 蒙版值域上限 (0-255)")
    skip_existing: bool = Field(True, description="跳过已有蒙版文件")


class MaskGenerateResponse(BaseModel):
    """蒙版生成任务启动响应. """

    ok: bool = True
    job_id: str


@router.post("/mask-generate")
async def mask_generate(request: Request, body: MaskGenerateBody) -> MaskGenerateResponse:
    """启动蒙版生成任务 (BiRefNet 或 SAM3 概念分割).

    依据 ``mode`` 分发:
    - ``"birefnet"``: 背景移除蒙版, 使用 BiRefNet.
    - ``"sam3"``: 概念分割蒙版, 需 ``expression`` 逻辑表达式组合概念与默认蒙版.

    单图模式 ``path`` 仅处理该图; 批量模式处理 ``dir`` 目录下全部图片.

    Callers:
        - `static/js/paint.js`
    """
    rt = get_runtime(request)
    if body.mode not in MASK_MODES:
        raise HTTPException(400, f"未知生成方式: {body.mode}")

    if body.path:
        src = resolve_path(rt.root, body.path)
        if not src.is_file() or src.suffix.lower() not in IMAGE_EXTS:
            raise HTTPException(400, f"无效图片路径: {body.path}")
        image_rel_paths = [body.path]
    else:
        image_rel_paths = collect_images(rt.root, subdir=body.dir)
    if not image_rel_paths:
        raise HTTPException(400, "没有可处理的图片")

    expression = body.expression or ""
    if body.mode == "sam3":
        if not expression.strip():
            raise HTTPException(400, "SAM3 模式需要逻辑表达式")
        try:
            ast = parse(expression)
        except ExpressionError as e:
            raise HTTPException(400, f"表达式错误: {e}")
        if not extract_concepts(ast):
            raise HTTPException(
                400,
                "表达式至少需要一个概念, 例如 (默认蒙板-(\"白色背景\"+\"对话气泡\"))",
            )

    prog = Sam3MaskProgress() if body.mode == "sam3" else BirefnetProgress()

    def run(emit: EmitFn) -> None:
        """工作线程入口.

        Callers:
            - `JobManager.submit`
        """
        def on_progress() -> None:
            emit(prog.snapshot())

        if body.mode == "sam3":
            run_sam3_expression_mask(
                image_rel_paths=[(rel, rt.root / rel) for rel in image_rel_paths],
                mask_dir=rt.mask_dir,
                expression=expression,
                skip_existing=body.skip_existing,
                progress=prog,
                on_progress=on_progress,
            )
        else:
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

    job_id = await jobs(request).submit(
        kind="mask_generate",
        group="compute",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    return MaskGenerateResponse(job_id=job_id)
