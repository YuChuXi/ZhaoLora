"""自然语言描述标注 API(数据集作用域). """

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from routes.deps import get_runtime, jobs
from services.analysis.caption import (
    RULE_TEMPLATES,
    SYSTEM_PROMPT_TEMPLATES,
    CaptionProgress,
    load_captions,
    ollama_status,
    rule_template_fields,
    run_caption,
)
from services.analysis.wd14 import DEFAULT_THRESHOLD
from services.core.jobs import EmitFn
from services.core.scanner import scan_images
from services.core.thumbnailer import image_size
from services.core.work_dir import list_work_dirs
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
from services.tagging.stats import resolve_selected_dirs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/caption", tags=["caption"])


@router.get("/dirs")
def dirs(request: Request) -> CaptionDirsResponse:
    """列出可描述的工作目录与描述服务状态.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.

    Returns:
        CaptionDirsResponse: 含工作目录列表、服务就绪状态与可用模式/注入范围.

    Callers:
        - ``static/js/nav.js:dirsApi``
        - ``static/js/caption.js:init``
    """
    rt = get_runtime(request)
    ok, info = ollama_status()
    return CaptionDirsResponse(
        dirs=list_work_dirs(rt.root),
        ready=ok,
        info=info,
        rule_templates=[
            CaptionRuleTemplateInfo(
                label=label,
                fields=rule_template_fields(source),
                text=source,
            )
            for label, source in RULE_TEMPLATES.items()
        ],
        default_wd14_threshold=DEFAULT_THRESHOLD,
    )


def _build_item(
    root: Path,
    work_dir: Path,
    dir_name: str,
    dir_rel: str,
    img_path: str,
    img_stem: str,
    captions: dict[str, list[str]],
    thumbnailer,
) -> CaptionImageItem:
    """构建描述页单张图片条目.

    Args:
        root (Path): 数据集根目录.
        work_dir (Path): 图片所在目录绝对路径(用于定位 ``captions.json``).
        dir_name (str): 目录名(前端展示用, 根目录为 ``.``).
        dir_rel (str): 图片所在目录相对数据集根的路径.
        img_path (str): 图片相对数据集根的路径.
        img_stem (str): 图片 stem.
        captions (dict[str, list[str]]): 该工作目录的 ``captions.json`` 内容.
        thumbnailer: 缩略图管理器.

    Returns:
        CaptionImageItem: 含尺寸、缩略图 hash 与现有描述的条目.

    Callers:
        - ``routes/caption.py:list_images``
    """
    abs_img = root / img_path
    width, height = image_size(abs_img) if abs_img.is_file() else (0, 0)
    return CaptionImageItem(
        path=img_path,
        stem=img_stem,
        width=width,
        height=height,
        thumbnail_hash=thumbnailer.hash_for(img_path),
        dir=dir_name,
        captions=captions.get(Path(img_path).name, []),
    )


@router.post("/list")
def list_images(request: Request, body: CaptionListBody) -> CaptionListResponse:
    """列出所选目录内图片及其 ``captions.json`` 现状.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (CaptionListBody): 目录列表请求.

    Returns:
        CaptionListResponse: 图片条目列表与已描述数量.

    Raises:
        HTTPException: 未选择目录时返回 400.

    Callers:
        - ``static/js/caption.js:refresh``
    """
    rt = get_runtime(request)
    root = rt.root
    selected = resolve_selected_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    thumbnailer = rt.thumbnailer
    items: list[CaptionImageItem] = []
    for work in selected:
        if work == root:
            dir_name, prefix, dir_rel = ".", "", "."
        else:
            dir_name, prefix, dir_rel = work.name, work.name, work.name
        store = load_captions(work)
        for img in scan_images(work, path_prefix=prefix):
            items.append(
                _build_item(
                    root, work, dir_name, dir_rel, img.path, img.stem, store, thumbnailer
                )
            )

    described = sum(1 for it in items if it.captions)
    return CaptionListResponse(
        items=items,
        total=len(items),
        described=described,
        dirs=[d.name if d != root else "." for d in selected],
    )


@router.post("/start")
async def start(request: Request, body: CaptionBody) -> JobSubmitResponse:
    """提交批量描述标注长任务, 返回 job_id.

    进度经 SSE 推送(见 ``GET /api/jobs/{job_id}/events``); 任务完成时快照的
    ``result`` 为 ``CaptionResult``.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (CaptionBody): 描述参数, 含目录、模式、注入范围、预设提示词与触发词表.

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度.

    Raises:
        HTTPException: Ollama 不可用、未选择目录、模式或注入范围非法时返回 400.

    Callers:
        - ``static/js/caption.js:startJob``
    """
    rt = get_runtime(request)
    root = rt.root
    ok, msg = ollama_status()
    if not ok:
        raise HTTPException(400, msg)

    selected = resolve_selected_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    modes = [m for m in body.modes if m in SYSTEM_PROMPT_TEMPLATES]
    if not modes:
        raise HTTPException(400, f"描述模式非法: {body.modes}")

    prog = CaptionProgress()
    payload = body

    def run(emit: EmitFn) -> CaptionResult:
        """工作线程: 逐张调用 Ollama 生成描述并写入 captions.json.

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

        result = run_caption(
            root,
            selected,
            modes=modes,
            general_threshold=payload.general_threshold,
            character_threshold=payload.character_threshold,
            rules=payload.rules,
            triggers=payload.triggers,
            force=payload.force,
            progress=prog,
            on_update=on_update,
        )
        emit(prog.snapshot().model_copy(update={"result": result}))
        return result

    job_id = await jobs(request).submit(
        kind="caption",
        group="compute",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    logger.info(
        "描述标注任务提交 dataset=%s dirs=%s modes=%s 注入通用=%s 注入角色=%s force=%s job_id=%s",
        rt.name,
        [d.name if d != root else "." for d in selected],
        modes,
        payload.general_threshold,
        payload.character_threshold,
        payload.force,
        job_id,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)
