"""视频 API —— 列表 / 封面 / 抽帧 / 保存帧 / 删除帧 / 位流帧类型分析长任务.

视频存放于数据集根 ``video`` 目录; 保存帧写入数据集 ``main`` 目录。
CPU/IO 密集操作经 ``asyncio.to_thread`` 移入线程池, 避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from routes.deps import get_runtime, jobs, resolve_path
from services.core.jobs import EmitFn
from services.core.video_frame import (
    VideoFrameError,
    VideoFrameStore,
    total_frames,
)
from services.core.video_scanner import (
    COVER_DIRNAME,
    VIDEO_DIRNAME,
    VideoScanError,
    cover_hash,
    ensure_cover,
    probe_video,
    scan_videos,
)
from services.models import (
    FrameSaveBody,
    FrameSaveResponse,
    JobSnapshot,
    JobSubmitResponse,
    VideoDetail,
    VideoInfo,
)
from services.core.work_dir import resolve_subdir

router = APIRouter(tags=["video"])


def _services(request: Request) -> tuple[Path, bool]:
    """取当前数据集根并确保 video / main 目录存在.

    Returns:
        ``(root, is_ready)``; 数据集无 video 目录时 ``is_ready`` 为 False.

    Callers:
        - 本模块各端点
    """
    rt = get_runtime(request)
    root = rt.root
    video_dir = root / VIDEO_DIRNAME
    if not video_dir.is_dir():
        return root, False
    return root, True


def _stores(request: Request):
    """构造视频帧服务与封面缓存目录.

    Returns:
        ``(VideoFrameStore, cover_dir)``.

    Callers:
        - 本模块各端点
    """
    rt = get_runtime(request)
    root = rt.root
    _, main_dir = resolve_subdir(root, "main")
    return VideoFrameStore(root, main_dir), root / COVER_DIRNAME


@router.get("/videos")
async def list_videos(request: Request) -> list[VideoInfo]:
    """列出 ``video`` 目录下视频并返回封面 hash.

    封面不在此处批量生成; 前端经 ``/video/cover`` 按需请求, 单张封面
    生成失败只影响该卡片, 不会导致整个列表加载失败.

    Callers:
        - `static/js/booru_filter.js` 视频来源
    """
    root, ready = _services(request)
    if not ready:
        return []
    items = scan_videos(root / VIDEO_DIRNAME, path_prefix=VIDEO_DIRNAME)
    for item in items:
        item.cover_hash = cover_hash(item.path)
    return items


@router.get("/video/cover")
async def video_cover(
    request: Request,
    path: str = Query(..., description="相对数据集根的视频路径"),
) -> FileResponse:
    """返回视频封面图(missing 时即时生成). 引用: 前端视频瀑布流. """
    root = get_runtime(request).root
    video_path = resolve_path(root, path)
    cover_dir = root / COVER_DIRNAME
    try:
        cover = await asyncio.to_thread(ensure_cover, video_path, cover_dir, path)
    except VideoScanError as e:
        raise HTTPException(502, str(e)) from e
    return FileResponse(
        cover,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/video/detail")
async def video_detail(
    request: Request,
    path: str = Query(..., description="相对数据集根的视频路径"),
) -> VideoDetail:
    """返回视频抽帧页详情(宽高/总帧数/已评分/已保存帧). 引用: 抽帧页. """
    root = get_runtime(request).root
    resolve_path(root, path)  # 校验路径合法
    store, _ = _stores(request)
    w, h, dur = probe_video(root / path.lstrip("/"))
    total = total_frames(path, root)
    return VideoDetail(
        path=path,
        name=Path(path).name,
        width=w,
        height=h,
        duration=dur,
        total=total,
        types=store.load_bitstream(path),
        saved=store.saved_indices(path),
    )


@router.get("/video/frame")
async def video_frame(
    request: Request,
    path: str = Query(...),
    index: int = Query(..., ge=0),
) -> Response:
    """按帧索引返回一帧 JPEG. 引用: 抽帧页预览. """
    rt = get_runtime(request)
    resolve_path(rt.root, path)
    store, _ = _stores(request)
    try:
        data = await asyncio.to_thread(store.frame_bytes, path, index, fmt="jpg")
    except VideoFrameError as e:
        raise HTTPException(400, str(e)) from e
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/video/frame/save")
async def save_frame(request: Request, body: FrameSaveBody) -> FrameSaveResponse:
    """保存指定帧为 PNG 到 ``main`` 目录. 引用: 抽帧页左键. """
    rt = get_runtime(request)
    resolve_path(rt.root, body.path)
    store, _ = _stores(request)
    try:
        await asyncio.to_thread(store.save_frame, body.path, body.index)
    except VideoFrameError as e:
        raise HTTPException(400, str(e)) from e
    saved = store.saved_indices(body.path)
    return FrameSaveResponse(path=body.path, index=body.index, saved=saved, count=len(saved))


@router.post("/video/frame/delete")
async def delete_frame(request: Request, body: FrameSaveBody) -> FrameSaveResponse:
    """删除已保存的指定帧 PNG. 引用: 抽帧页右键. """
    rt = get_runtime(request)
    resolve_path(rt.root, body.path)
    store, _ = _stores(request)
    await asyncio.to_thread(store.remove_frame, body.path, body.index)
    saved = store.saved_indices(body.path)
    return FrameSaveResponse(path=body.path, index=body.index, saved=saved, count=len(saved))


@router.post("/video/analysis/start")
async def start_analysis(request: Request, path: str = Query(...)) -> JobSubmitResponse:
    """启动视频位流帧类型分析长任务(ffprobe 提取每帧 I/P/B 类型与包大小).

    进度见 GET /api/jobs/{id}/events。Callers: 抽帧页.
    """
    rt = get_runtime(request)
    resolve_path(rt.root, path)
    store, _ = _stores(request)
    total = total_frames(path, rt.root)
    if total <= 0:
        raise HTTPException(400, "视频无法读取或总帧数为 0")

    cancel_flag = {"on": False}

    def run(emit: EmitFn) -> None:
        """分析工作线程入口. 引用: ``JobManager.submit``. """
        emit(
            JobSnapshot(
                status="running", phase="analysis", total=total, done=0, pct=0,
                message="正在分析帧类型",
            )
        )

        def progress(done: int, total_n: int) -> None:
            """进度回调. 引用: ``analyze_bitstream``. """
            pct = round(100.0 * done / total_n, 1) if total_n else 100.0
            emit(
                JobSnapshot(
                    status="running", phase="analysis", total=total_n, done=done, pct=pct,
                    message=f"正在分析帧类型 {done}/{total_n}",
                )
            )

        store.analyze_bitstream(
            path,
            total,
            cancel_fn=lambda: cancel_flag["on"],
            emit=progress,
        )
        emit(
            JobSnapshot(
                status="done", phase="done", total=total, done=total, pct=100,
                message=f"帧类型分析完成 {total} 帧",
            )
        )

    job_id = await jobs(request).submit(
        kind="video_analysis",
        group="compute",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: cancel_flag.update(on=True),
    )
    return JobSubmitResponse(ok=True, job_id=job_id)