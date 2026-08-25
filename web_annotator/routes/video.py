"""视频 API —— 列表 / 封面 / 抽帧 / 保存帧 / 删除帧 / 位流帧类型分析长任务.

视频存放于数据集根 ``video`` 目录; 保存帧写入数据集 ``main`` 目录。
CPU/IO 密集操作经 ``asyncio.to_thread`` 移入线程池, 避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from routes.deps import get_runtime, jobs, resolve_path
from services.core.jobs import EmitFn
from services.core.video_frame import (
    VideoFrameError,
    VideoFrameStore,
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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["video"])


def _services(request: Request) -> tuple[Path, bool]:
    """取当前数据集根并确保 video / main 目录存在.

    Returns:
        ``(root, is_ready)``; 数据集无 video 目录时 ``is_ready`` 为 False.

    Callers:
        - ``routes/video.py:list_videos``
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
        - ``routes/video.py:video_detail``
        - ``routes/video.py:video_frame``
        - ``routes/video.py:video_segment``
        - ``routes/video.py:save_frame``
        - ``routes/video.py:delete_frame``
        - ``routes/video.py:start_analysis``
    """
    rt = get_runtime(request)
    root = rt.root
    _, main_dir = resolve_subdir(root, "main")
    return VideoFrameStore(root, main_dir), root / COVER_DIRNAME


@router.get("/videos")
async def list_videos(
    request: Request,
    probe: bool = Query(False, description="是否并行 ffprobe 探测宽高/时长"),
) -> list[VideoInfo]:
    """列出 ``video`` 目录下视频并返回封面 hash.

    ``probe=False`` 时仅列文件(快); ``probe=True`` 时并行 ffprobe 探测宽高/时长。
    封面不在此处批量生成; 前端经 ``/video/cover`` 按需请求。

    Callers:
        - ``static/js/booru_filter.js:`` 视频来源
        - ``static/js/video_extract.js:`` A/D 视频切换
    """
    root, ready = _services(request)
    if not ready:
        return []
    items = await asyncio.to_thread(
        scan_videos, root / VIDEO_DIRNAME, path_prefix=VIDEO_DIRNAME, probe=probe,
    )
    for item in items:
        item.cover_hash = cover_hash(item.path)
    return items


@router.get("/video/cover")
async def video_cover(
    request: Request,
    path: str = Query(..., description="相对数据集根的视频路径"),
) -> FileResponse:
    """返回视频封面图(missing 时即时生成).

    Callers:
        - ``static/js/booru_filter.js:`` 视频卡片封面
    """
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
    """返回视频抽帧页详情(宽高/总帧数/已保存帧/位流类型).

    Callers:
        - ``static/js/video_extract.js:`` 加载视频详情
    """
    root = get_runtime(request).root
    resolve_path(root, path)  # 校验路径合法
    store, _ = _stores(request)
    w, h, dur, total = probe_video(root / path.lstrip("/"))
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
    size: int | None = Query(None, ge=1),
) -> Response:
    """按帧索引返回一帧 JPEG, 可指定 ``size`` 等比缩略最长边.

    Callers:
        - ``static/js/video_extract.js:`` 缩略图条
    """
    rt = get_runtime(request)
    resolve_path(rt.root, path)
    store, _ = _stores(request)
    try:
        data = await asyncio.to_thread(
            store.frame_bytes, path, index, fmt="jpg", max_size=size
        )
    except VideoFrameError as e:
        raise HTTPException(400, str(e)) from e
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/video/segment")
async def video_segment(
    request: Request,
    path: str = Query(...),
    start: int = Query(..., ge=0),
    end: int = Query(..., ge=0),
    hide_b: bool = Query(False),
) -> Response:
    """返回 ``[start, end]`` 帧窗口的 H.264 Annex-B 裸流.

    ``hide_b`` 为真时 ``-bf 0`` 禁用 B 帧(解码顺序 = 显示顺序); 否则正常
    I/P/B 编码。首帧为关键帧, 段自包含可从头解码。实际帧数通过
    ``X-Frame-Count`` 响应头告知前端(视频末尾可能少于请求数).

    Callers:
        - ``static/js/video_extract.js:`` 主预览 WebCodecs 解码
    """
    rt = get_runtime(request)
    resolve_path(rt.root, path)
    store, _ = _stores(request)
    if end < start:
        raise HTTPException(400, "end 不能小于 start")
    try:
        data, count = await asyncio.to_thread(store.segment_bytes, path, start, end, hide_b=hide_b)
    except VideoFrameError as e:
        raise HTTPException(400, str(e)) from e
    return Response(
        content=data,
        media_type="video/h264",
        headers={"Cache-Control": "no-store", "X-Frame-Count": str(count)},
    )


@router.post("/video/frame/save")
async def save_frame(request: Request, body: FrameSaveBody) -> FrameSaveResponse:
    """保存指定帧为 PNG 到 ``main`` 目录.

    Callers:
        - ``static/js/video_extract.js:`` 左键/存帧按钮
    """
    rt = get_runtime(request)
    resolve_path(rt.root, body.path)
    store, _ = _stores(request)
    try:
        await asyncio.to_thread(store.save_frame, body.path, body.index)
    except VideoFrameError as e:
        raise HTTPException(400, str(e)) from e
    saved = store.saved_indices(body.path)
    logger.info(
        "数据集 %s 保存视频帧: 视频=%s 帧索引=%d 已存%d帧",
        request.state.runtime.root.name, body.path, body.index, len(saved),
    )
    return FrameSaveResponse(path=body.path, index=body.index, saved=saved, count=len(saved))


@router.post("/video/frame/delete")
async def delete_frame(request: Request, body: FrameSaveBody) -> FrameSaveResponse:
    """删除已保存的指定帧 PNG.

    Callers:
        - ``static/js/video_extract.js:`` 右键/删帧按钮
    """
    rt = get_runtime(request)
    resolve_path(rt.root, body.path)
    store, _ = _stores(request)
    await asyncio.to_thread(store.remove_frame, body.path, body.index)
    saved = store.saved_indices(body.path)
    logger.info(
        "数据集 %s 删除视频帧: 视频=%s 帧索引=%d 剩余%d帧",
        request.state.runtime.root.name, body.path, body.index, len(saved),
    )
    return FrameSaveResponse(path=body.path, index=body.index, saved=saved, count=len(saved))


@router.post("/video/analysis/start")
async def start_analysis(request: Request, path: str = Query(...)) -> JobSubmitResponse:
    """启动视频位流帧类型分析长任务(ffprobe 提取每帧 I/P/B 类型与包大小).

    进度见 GET /api/jobs/{id}/events。

    Callers:
        - ``static/js/video_extract.js:`` 帧类型分析按钮
    """
    rt = get_runtime(request)
    resolve_path(rt.root, path)
    store, _ = _stores(request)
    _w, _h, _dur, total = probe_video(rt.root / path.lstrip("/"))
    if total <= 0:
        raise HTTPException(400, "视频无法读取或总帧数为 0")

    cancel_flag = {"on": False}

    def run(emit: EmitFn) -> None:
        """分析工作线程入口.

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        emit(
            JobSnapshot(
                status="running", phase="analysis", total=total, done=0, pct=0,
                message="正在分析帧类型",
            )
        )

        def progress(done: int, total_n: int) -> None:
            """进度回调.

            Callers:
                - ``services/core/video_frame.py:VideoFrameStore.analyze_bitstream``
            """
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
    logger.info(
        "数据集 %s 启动帧类型分析: job=%s 视频=%s 共%d帧",
        request.state.runtime.root.name, job_id, path, total,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)
