"""自然语言描述标注 API(数据集作用域). """

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from routes.deps import DatasetDep, jobs, resolve_path
from services.analysis.caption import (
    FILTER_PRESETS,
    RULE_TEMPLATES,
    SYSTEM_PROMPT_TEMPLATES,
    load_captions,
    openai_status,
    run_caption,
    write_captions,
)
from services.analysis.wd14 import DEFAULT_THRESHOLD
from services.core.jobs import EmitFn, JobProgress
from services.core.thumbnailer import image_size
from services.dataset.workdir import WorkImage
from services.models import (
    CaptionBody,
    CaptionDeleteBody,
    CaptionDeleteResponse,
    CaptionDirsResponse,
    CaptionFilterPreset,
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
    ok, info = openai_status()
    return CaptionDirsResponse(
        dirs=[w.to_info() for w in ds.work_dirs()],
        ready=ok,
        info=info,
        rule_templates=[
            CaptionRuleTemplateInfo(label=label, text=source)
            for label, source in RULE_TEMPLATES.items()
        ],
        filter_presets=[
            CaptionFilterPreset(label=label, mode=mode, pattern=pattern)
            for label, (mode, pattern) in FILTER_PRESETS.items()
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
        CaptionListResponse: 图片条目列表.

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

    return CaptionListResponse(
        items=items,
        dirs=[w.name for w in selected],
    )


@router.post("/delete")
def delete_many(ds: DatasetDep, body: CaptionDeleteBody) -> CaptionDeleteResponse:
    """删除多张图片在各自目录 ``captions.json`` 中的描述条目.

    同一工作目录的删除合并为一次整体写入; 任一路径不在数据集内或图片不存在时整批
    拒绝, 不产生部分写入.

    Args:
        ds (Dataset): 当前数据集, 由路由依赖注入.
        body (CaptionDeleteBody): 待删除描述的图片相对路径列表.

    Returns:
        CaptionDeleteResponse: 实际删除描述条目的图片数量.

    Raises:
        HTTPException: 路径越界返回 400, 图片不存在返回 404.

    Callers:
        - ``static/js/caption.js:deleteSelectedCaptions``
        - ``static/js/caption.js:deleteViewedCaption``
    """
    stores: dict[Path, dict[str, list[str]]] = {}
    deleted = 0
    for raw in body.paths:
        rel = raw.replace("\\", "/").lstrip("/")
        img_abs = resolve_path(ds, rel)
        if not img_abs.is_file():
            raise HTTPException(404, f"图片不存在: {rel}")
        store = stores.setdefault(img_abs.parent, load_captions(img_abs.parent))
        if store.pop(img_abs.name, None) is not None:
            deleted += 1
    for parent, store in stores.items():
        write_captions(parent, store)
    logger.info(
        "批量删描述 dataset=%s 请求=%d 实际删除=%d", ds.name, len(body.paths), deleted
    )
    return CaptionDeleteResponse(deleted=deleted)


@router.post("/start")
async def start(ds: DatasetDep, request: Request, body: CaptionBody) -> JobSubmitResponse:
    """提交批量描述标注长任务, 返回 job_id.

    进度经 SSE 推送(见 ``GET /api/jobs/{job_id}/events``); 任务完成时快照的
    ``result`` 为 ``CaptionResult``.

    Args:
        ds (Dataset): 当前数据集, 由路由依赖注入.
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (CaptionBody): 描述参数, 含目录或显式图片路径、模式、注入范围与预设提示词.

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度.

    Raises:
        HTTPException: 描述服务不可用、未选择目录、图片不在所选目录内、模式或注入
            范围非法时返回 400.

    Callers:
        - ``static/js/caption.js:startJob``
    """
    ok, msg = openai_status()
    if not ok:
        raise HTTPException(400, msg)

    selected = ds.select(body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    modes = [m for m in body.modes if m in SYSTEM_PROMPT_TEMPLATES]
    if not modes:
        raise HTTPException(400, f"描述模式非法: {body.modes}")

    if body.paths:
        available = {p.relative_to(ds.root).as_posix() for w in selected for p in w.image_files()}
        unknown = [p for p in body.paths if p not in available]
        if unknown:
            raise HTTPException(400, f"图片不在所选目录内: {', '.join(unknown[:5])}")

    prog = JobProgress()
    payload = body

    async def run(emit: EmitFn) -> CaptionResult:
        """在事件循环中逐张调用描述服务生成描述并写入 captions.json.

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
            paths=set(body.paths) if body.paths else None,
            modes=modes,
            general_threshold=payload.general_threshold,
            character_threshold=payload.character_threshold,
            nsfw_threshold=payload.nsfw_threshold,
            rules=payload.rules,
            force=payload.force,
            progress=prog,
            on_update=on_update,
        )
        prog.result = result
        emit(prog.snapshot())
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
