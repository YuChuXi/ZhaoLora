"""自然语言描述标注 API(数据集作用域). """

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from routes.deps import DatasetDep, jobs
from services.analysis.caption import (
    RULE_TEMPLATES,
    SYSTEM_PROMPT_TEMPLATES,
    CaptionProgress,
    load_captions,
    ollama_status,
    run_caption,
)
from services.analysis.wd14 import DEFAULT_THRESHOLD
from services.core.jobs import EmitFn
from services.core.thumbnailer import image_size
from services.dataset.workdir import WorkImage
from services.models import (
    CaptionBody,
    CaptionDirsResponse,
    CaptionImageItem,
    CaptionListBody,
    CaptionListResponse,
    CaptionResult,
    CaptionRuleTemplateInfo,
    JobSubmitResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/caption", tags=["caption"])


@router.get("/dirs")
def dirs(ds: DatasetDep) -> CaptionDirsResponse:
    """列出可描述的工作目录与描述服务状态.

    Args:
        ds (Dataset): 当前数据集, 由路由依赖注入.

    Returns:
        CaptionDirsResponse: 含工作目录列表、服务就绪状态与可用模式/注入范围.

    Callers:
        - ``static/js/nav.js:dirsApi``
        - ``static/js/caption.js:init``
    """
    ok, info = ollama_status()
    return CaptionDirsResponse(
        dirs=[w.to_info() for w in ds.work_dirs()],
        ready=ok,
        info=info,
        rule_templates=[
            CaptionRuleTemplateInfo(label=label, text=source)
            for label, source in RULE_TEMPLATES.items()
        ],
        default_wd14_threshold=DEFAULT_THRESHOLD,
    )


def _build_item(
    img: WorkImage,
    captions: dict[str, list[str]],
    thumbnailer,
) -> CaptionImageItem:
    """构建描述页单张图片条目.

    Args:
        img (WorkImage): 图片及其工作目录.
        captions (dict[str, list[str]]): 该工作目录的 ``captions.json`` 内容.
        thumbnailer: 缩略图管理器.

    Returns:
        CaptionImageItem: 含尺寸、缩略图 hash 与现有描述的条目.

    Callers:
        - ``routes/caption.py:list_images``
    """
    width, height = image_size(img.abs) if img.abs.is_file() else (0, 0)
    return CaptionImageItem(
        path=img.rel,
        stem=img.stem,
        width=width,
        height=height,
        thumbnail_hash=thumbnailer.hash_for(img.rel),
        dir=img.work.name,
        captions=captions.get(img.abs.name, []),
    )


@router.post("/list")
def list_images(ds: DatasetDep, body: CaptionListBody) -> CaptionListResponse:
    """列出所选目录内图片及其 ``captions.json`` 现状.

    Args:
        ds (Dataset): 当前数据集, 由路由依赖注入.
        body (CaptionListBody): 目录列表请求.

    Returns:
        CaptionListResponse: 图片条目列表与已描述数量.

    Raises:
        HTTPException: 未选择目录时返回 400.

    Callers:
        - ``static/js/caption.js:refresh``
    """
    selected = ds.select(body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    thumbnailer = ds.thumbnailer
    items: list[CaptionImageItem] = []
    for work in selected:
        store = load_captions(work.path)
        for img in work.images():
            items.append(_build_item(img, store, thumbnailer))

    described = sum(1 for it in items if it.captions)
    return CaptionListResponse(
        items=items,
        total=len(items),
        described=described,
        dirs=[w.name for w in selected],
    )


@router.post("/start")
async def start(ds: DatasetDep, request: Request, body: CaptionBody) -> JobSubmitResponse:
    """提交批量描述标注长任务, 返回 job_id.

    进度经 SSE 推送(见 ``GET /api/jobs/{job_id}/events``); 任务完成时快照的
    ``result`` 为 ``CaptionResult``.

    Args:
        ds (Dataset): 当前数据集, 由路由依赖注入.
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (CaptionBody): 描述参数, 含目录、模式、注入范围与预设提示词.

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度.

    Raises:
        HTTPException: Ollama 不可用、未选择目录、模式或注入范围非法时返回 400.

    Callers:
        - ``static/js/caption.js:startJob``
    """
    ok, msg = ollama_status()
    if not ok:
        raise HTTPException(400, msg)

    selected = ds.select(body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    modes = [m for m in body.modes if m in SYSTEM_PROMPT_TEMPLATES]
    if not modes:
        raise HTTPException(400, f"描述模式非法: {body.modes}")

    prog = CaptionProgress()
    payload = body

    async def run(emit: EmitFn) -> CaptionResult:
        """在事件循环中逐张调用 Ollama 生成描述并写入 captions.json.

        Args:
            emit (EmitFn): 任务事件推送回调.

        Returns:
            CaptionResult: 本次标注结果.

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        def on_update() -> None:
            """推送描述进度快照到 SSE 通道."""
            emit(prog.snapshot())

        result = await run_caption(
            selected,
            modes=modes,
            general_threshold=payload.general_threshold,
            character_threshold=payload.character_threshold,
            nsfw_threshold=payload.nsfw_threshold,
            rules=payload.rules,
            force=payload.force,
            progress=prog,
            on_update=on_update,
        )
        emit(prog.snapshot().model_copy(update={"result": result}))
        return result

    job_id = await jobs(request).submit(
        kind="caption",
        group="compute",
        dataset=ds.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    logger.info(
        "描述标注任务提交 dataset=%s dirs=%s modes=%s 注入通用=%s 注入角色=%s NSFW=%s force=%s job_id=%s",
        ds.name,
        [w.name for w in selected],
        modes,
        payload.general_threshold,
        payload.character_threshold,
        payload.nsfw_threshold,
        payload.force,
        job_id,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)
