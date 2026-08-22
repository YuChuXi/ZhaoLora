"""在线来源 API: 搜索 / 预览代理 / 下载到工作目录. """

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from services.core.video_scanner import VIDEO_DIRNAME

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from curl_cffi.requests.exceptions import RequestException as CffiError

from services.online_sources.batch import BatchProgress, get_progress, register_progress, run_batch
from services.online_sources import (
    download_post,
    get_source,
    is_downloaded,
    list_source_names,
    local_rel_path,
    regenerate_captions as _regenerate_captions,
    remove_download,
    search_posts,
)
from services.online_sources.framework.http import HttpClient, NetworkError as _NetworkError
from routes.deps import get_runtime, jobs
from services.core.jobs import EmitFn
from services.models import (
    BatchParamsBody,
    BatchStartBody,
    BooruPost,
    BooruDownloadResult,
    BooruSearchResult,
    BooruSourcesResponse,
    BooruUndownloadResult,
    JobSnapshot,
    JobSubmitResponse,
)
from services.core.scanner import IMAGE_EXTS
from services.core.work_dir import resolve_subdir
from services.online_sources.framework.base import search_session_posts as _search_session_posts

router = APIRouter(prefix="/online-sources", tags=["online-sources"])

_NETWORK_ERRORS = (RuntimeError, OSError, _NetworkError, CffiError)


class SearchBody(BaseModel):
    """Booru 搜索请求体."""

    source: str = Field(..., description="gelbooru | danbooru")
    tags: str = ""
    page: int = Field(1, ge=1, le=10000)
    session: str = Field("", description="会话式来源的续拉 token; 新搜索留空")
    order: str = Field("", description="空 | score | id | id_asc")
    extra: str = Field("", description="额外元标签, 如 rating:g width:>=1920")
    dir: str = Field("main", description="用于标记已下载的工作子目录")


class DownloadBody(BaseModel):
    """Booru 下载 / 取消下载请求体."""

    source: str
    id: str = Field(..., min_length=1)
    filename: str = ""
    dir: str = Field("main", description="数据集根下的工作子目录")
    overwrite: bool = False
    post: dict | None = Field(None, description="前端搜索阶段持有的帖子, 供下载直接取用")


@router.get("/sources")
def list_sources() -> BooruSourcesResponse:
    """列出可用图站与代理。

    Returns:
        BooruSourcesResponse: 含来源名列表与代理地址。
    """
    proxy = os.environ.get("HTTP_PROXY", "") or os.environ.get("HTTPS_PROXY", "")
    return BooruSourcesResponse(
        sources=list_source_names(),
        proxy=proxy.strip(),
    )


@router.get("/tag-suggest")
async def tag_suggest(
    source: str = Query(..., description="gelbooru | danbooru"),
    q: str = Query(..., min_length=1, max_length=100, description="标签前缀"),
    limit: int = Query(10, ge=1, le=50),
) -> dict:
    """标签自动补全。

    Args:
        source (str): 来源名(gelbooru | danbooru)。
        q (str): 标签前缀, 长度 1-100。
        limit (int): 返回数量上限, 1-50。

    Returns:
        dict: ``{"tags": list[str]}``, 补全候选标签列表。

    Callers:
        - ``static/js/booru_filter.js:onInput``
    """
    try:
        src = get_source(source)
    except ValueError:
        raise HTTPException(404, f"未知来源: {source}") from None
    try:
        tags = await src.autocomplete(q, limit)
    except _NETWORK_ERRORS as e:
        raise HTTPException(502, f"补全失败: {e}") from e
    return {"tags": tags}


@router.post("/search")
async def search(request: Request, body: SearchBody) -> BooruSearchResult:
    """搜索图站并标记本地下载状态。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (SearchBody): 搜索参数, 含来源、标签、分页、排序等。

    Returns:
        BooruSearchResult: 搜索结果, 含帖子列表与本地下载状态。

    Callers:
        - ``static/js/booru_filter.js:search``
    """
    source = body.source.strip().lower()
    src = get_source(source)
    order = (body.order or "").strip().lower()
    if order and src.valid_orders and order not in src.valid_orders:
        raise HTTPException(400, f"不支持的排序: {body.order}")
    query = src.build_search_query(body.tags, order=order, extra=body.extra or "")
    root = get_runtime(request).root
    try:
        _, out_dir = resolve_subdir(root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        if src.session_based:
            raw_posts, has_more, session = await _search_session_posts(
                source, body.tags, body.session or "", order=order, extra=body.extra or "",
            )
        else:
            raw_posts, has_more = await search_posts(
                source, body.tags, page=body.page,
                order=order, extra=body.extra or "",
            )
            session = ""
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except _NETWORK_ERRORS as e:
        raise HTTPException(502, f"搜索失败: {e}") from e

    posts: list[BooruPost] = []
    for post in raw_posts:
        post.downloaded = is_downloaded(out_dir, post.filename)
        posts.append(post)

    return BooruSearchResult(
        source=source,
        tags=body.tags.strip(),
        extra=(body.extra or "").strip(),
        order=order,
        query=query,
        dir=body.dir,
        page=body.page,
        count=len(posts),
        has_more=has_more,
        session=session,
        posts=posts,
    )


@router.get("/{source}-image")
async def proxy_image(
    source: str,
    url: str = Query(..., min_length=8, description="图床原图 URL"),
    id: str = Query(None, description="Gelbooru 帖 id, 用于合法性校验"),
    kind: str = Query("", description="thumb 或 sample"),
) -> StreamingResponse:
    """按来源代理图片(防盗链), 流式传输。

    图床白名单 / 防盗链头 / 超时均由各 ``SourceClient`` 自描述, 见
    ``SourceClient.proxy_image``。

    Args:
        source (str): 来源名。
        url (str): 图床原图 URL, 长度至少 8。
        id (str | None): Gelbooru 帖 id, 用于合法性校验。
        kind (str): 缩略图(thumb)或样本图(sample)。

    Returns:
        StreamingResponse: 流式图片响应, 含缓存头。

    Callers:
        - ``static/js/booru_filter.js:thumb``
        - ``static/js/booru_filter.js:sample``
    """
    try:
        src = get_source(source)
    except ValueError:
        raise HTTPException(404, f"未知来源: {source}") from None
    pid = (id or "").strip()
    if pid and ("/" in pid or "\\" in pid or ".." in pid):
        raise HTTPException(400, "非法 id")
    try:
        ctype, chunks = await src.proxy_image(url, kind=kind)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except _NetworkError as e:
        raise HTTPException(502, f"图片拉取失败: {e}") from e
    return StreamingResponse(
        chunks, media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _cancel_on_disconnect(request: Request, task: asyncio.Task) -> None:
    """客户端断开时取消正在执行的下载任务。

    下载网络 IO 全部位于 await 点, task.cancel() 会在此抛 CancelledError;
    同步写文件块要么完整执行要么未执行, 不会留下半截目标文件。

    Args:
        request (Request): 请求对象, 用于探测连接是否已断开。
        task (asyncio.Task): 封装的下载协程。

    Returns:
        None: 无返回值, 仅在断连时取消任务。

    Callers:
        - ``routes/online_sources.py:download``
    """
    while not task.done():
        if await request.is_disconnected():
            task.cancel()
            return
        await asyncio.sleep(0.2)


@router.post("/download")
async def download(
    request: Request, body: DownloadBody
) -> BooruDownloadResult | JobSubmitResponse:
    """下载帖子: 图片帖同步, 视频帖提交长任务。

    图片帖同步下载到 ``body.dir`` 子目录; 视频帖 (``post.is_video``) 提交为
    长任务异步下载到数据集 ``video`` 目录, 返回 ``JobSubmitResponse`` 供前端
    监听进度。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (DownloadBody): 下载参数, 含来源、帖子 id、目录、是否覆盖、帖子原始数据。

    Returns:
        BooruDownloadResult | JobSubmitResponse: 图片帖返回下载结果; 视频帖返回任务提交响应。

    Callers:
        - ``static/js/booru_filter.js:downloadOne``
    """
    source = body.source.strip().lower()
    if source not in list_source_names():
        raise HTTPException(400, f"不支持的源: {body.source}")
    root = get_runtime(request).root
    if not body.post:
        raise HTTPException(400, "缺少帖子数据 post")
    post = BooruPost.model_validate(body.post)

    if post.is_video:
        return await _submit_video_download(request, source, post, root)

    try:
        name, out_dir = resolve_subdir(root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    task = asyncio.create_task(
        download_post(
            source,
            post,
            out_dir,
            overwrite=body.overwrite,
        )
    )
    await _cancel_on_disconnect(request, task)
    try:
        result = task.result()
    except asyncio.CancelledError:
        raise HTTPException(499, "客户端已断开，下载已取消")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        msg = str(e)
        code = 429 if "429" in msg else 502
        raise HTTPException(code, f"下载失败: {e}") from e
    except (OSError, _NetworkError, CffiError) as e:
        raise HTTPException(502, f"下载失败: {e}") from e
    rel = local_rel_path(root, result.path)
    thumb = get_runtime(request).thumbnailer
    await thumb.ensure_async(rel)
    return result.model_copy(
        update={
            "dir": name,
            "filename": Path(result.path).name,
            "rel_path": rel,
            "thumbnail_hash": thumb.hash_for(rel),
            "path": rel,
            "tags_path": local_rel_path(root, Path(result.tags_path)),
        }
    )


async def _submit_video_download(
    request: Request, source: str, post: BooruPost, root: Path
) -> JobSubmitResponse:
    """提交视频下载长任务, 经 ``download_post`` 写入数据集 ``video`` 目录。

    工作线程内运行 ``download_post`` (视频帖进入对应来源的 ``download`` 视频
    分支, B 站用 BBDown, 其余来源下载原始文件); 异常由 ``JobManager`` 捕获并
    置为 error 状态, 本函数无需自行捕获处理异常。

    Args:
        request (Request): 请求对象。
        source (str): 来源名。
        post (BooruPost): 视频帖 (``is_video`` 为真)。
        root (Path): 数据集根目录。

    Returns:
        JobSubmitResponse: 任务提交结果, 前端据 ``job_id`` 监听进度。

    Raises:
        HTTPException: 已有进行中的视频下载任务时返回 409。

    Callers:
        - ``routes/online_sources.py:download``
    """
    rt = get_runtime(request)
    mgr = jobs(request)
    if mgr.find_active(dataset=rt.name, kind="video_download") is not None:
        raise HTTPException(409, "视频下载已在进行中")
    video_dir = root / VIDEO_DIRNAME

    def run(emit: EmitFn) -> None:
        """工作线程入口。

        Args:
            emit (EmitFn): 任务事件推送回调。

        Returns:
            None: 无返回值, 仅推送进度并完成下载。

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        def on_progress(pct: int, total: int) -> None:
            """把来源的 ``(pct, total)`` 进度转为 JobSnapshot 推送。

            Args:
                pct (int): 进度百分比。
                total (int): 总量。

            Returns:
                None: 无返回值, 仅推送快照。
            """
            emit(
                JobSnapshot(
                    status="running", phase="downloading",
                    total=total, done=pct, pct=pct,
                    message=f"正在下载视频 {pct}%",
                )
            )
        emit(
            JobSnapshot(
                status="running", phase="downloading", total=1, done=0, pct=0,
                message="正在下载视频",
            )
        )
        result = asyncio.run(
            download_post(source, post, video_dir, overwrite=False, emit=on_progress)
        )
        emit(
            JobSnapshot(
                status="done", phase="done", total=1, done=1, pct=100,
                message=f"视频下载完成: {Path(result.path).name}",
            )
        )

    job_id = await mgr.submit(
        kind="video_download",
        group="network",
        dataset=rt.name,
        run=run,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)


@router.post("/undownload")
def undownload(request: Request, body: DownloadBody) -> BooruUndownloadResult:
    """删除工作目录中该帖已下载文件。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (DownloadBody): 取消下载参数, 含来源、文件名、目录。

    Returns:
        BooruUndownloadResult: 含删除的图片列表与目录名。

    Callers:
        - ``static/js/booru_filter.js:undownloadOne``
    """
    source = body.source.strip().lower()
    if source not in list_source_names():
        raise HTTPException(400, f"不支持的源: {body.source}")
    root = get_runtime(request).root
    try:
        name, out_dir = resolve_subdir(root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        result = remove_download(source, body.filename.strip(), out_dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # 用 remove_download 返回的真实文件名清理缩略图缓存
    thumb = get_runtime(request).thumbnailer
    for img in result.images:
        path = thumb.get_path(f"{name}/{img}")
        if path.is_file():
            path.unlink()

    return result.model_copy(update={"dir": name})


@router.post("/batch/start")
async def batch_start(
    request: Request, body: BatchStartBody
) -> JobSubmitResponse:
    """提交批量下载长任务。

    进度见 GET /api/jobs/{id}/events；刷新后用 GET /api/jobs/active?kind=online_batch&dataset={dataset} 重连。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (BatchStartBody): 批量下载参数, 含来源、标签、分页、连接、限速等。

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度。

    Callers:
        - ``static/js/booru_filter.js:runBatch``
    """
    source = body.source.strip().lower()
    src = get_source(source)
    order = (body.order or "").strip().lower()
    if order and src.valid_orders and order not in src.valid_orders:
        raise HTTPException(400, f"不支持的排序: {body.order}")

    rt = get_runtime(request)
    mgr = jobs(request)
    existing = mgr.find_active(dataset=rt.name, kind="online_batch")
    if existing is not None:
        raise HTTPException(409, "批量下载已在进行中")

    try:
        _, out_dir = resolve_subdir(rt.root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    prog = BatchProgress(
        limit=body.limit,
        conn=body.conn,
        rate_rps=body.rate_rps,
        dataset=rt.name,
    )
    thumb = rt.thumbnailer
    tags = body.tags
    extra = body.extra or ""
    start_page = body.page
    root = rt.root
    anima_format = bool(body.anima_format)
    include_meta_tags = bool(body.include_meta_tags)
    keep_underscores = bool(body.keep_underscores)
    job_box: dict[str, str] = {"id": ""}

    def run(emit: EmitFn) -> None:
        """工作线程入口。

        Args:
            emit (EmitFn): 任务事件推送回调。

        Returns:
            None: 无返回值, 由 ``run_batch`` 内部推送进度。

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        run_batch(
            job_id=job_box["id"],
            source=source,
            tags=tags,
            extra=extra,
            order=order,
            out_dir=out_dir,
            root=root,
            start_page=start_page,
            progress=prog,
            emit=emit,
            thumbnailer=thumb,
            anima_format=anima_format,
            include_meta_tags=include_meta_tags,
            keep_underscores=keep_underscores,
        )

    job_id = await mgr.submit(
        kind="online_batch",
        group="network",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    job_box["id"] = job_id
    register_progress(job_id, prog)
    return JobSubmitResponse(ok=True, job_id=job_id)


@router.post("/batch/params")
def batch_params(request: Request, body: BatchParamsBody) -> JobSubmitResponse:
    """运行中更新数量 / 连接 / 限速。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (BatchParamsBody): 参数更新体, 含 limit/conn/rate_rps 可选字段。

    Returns:
        JobSubmitResponse: 含原 job_id, 便于前端续接进度。

    Callers:
        - ``static/js/booru_filter.js:pushBatchParams``
    """
    rt = get_runtime(request)
    job = jobs(request).find_active(dataset=rt.name, kind="online_batch")
    if job is None:
        raise HTTPException(404, "没有进行中的批量任务")
    prog = get_progress(job.id)
    if prog is None:
        raise HTTPException(404, "没有进行中的批量任务")
    if body.limit is not None:
        prog.limit = body.limit
    if body.conn is not None:
        prog.conn = body.conn
    if body.rate_rps is not None:
        prog.rate_rps = body.rate_rps
    return JobSubmitResponse(ok=True, job_id=job.id)


class RegenerateCaptionsBody(BaseModel):
    """批量重生成 caption 请求."""

    dir: str = Field("main", description="工作子目录")
    anima_format: bool = Field(True, description="Anima 格式")
    include_meta_tags: bool = Field(False, description="包含元数据标签")
    keep_underscores: bool = Field(False, description="保留下划线")


@router.post("/regenerate-captions")
async def regenerate_captions(request: Request, body: RegenerateCaptionsBody) -> dict:
    """从 .danbooru_meta / .gelbooru_meta 批量重生成旁路 .txt。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (RegenerateCaptionsBody): 重生成参数, 含目录、格式、元数据开关。

    Returns:
        dict: ``{"ok": bool, "count": int}``, 含成功标志与重生成数量。

    Callers:
        - ``static/js/booru_filter.js:regenerateCaptions``
    """
    rt = get_runtime(request)
    try:
        _, out_dir = resolve_subdir(rt.root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    count = await _regenerate_captions(
        out_dir,
        anima_format=body.anima_format,
        include_meta_tags=body.include_meta_tags,
        keep_underscores=body.keep_underscores,
    )
    return {"ok": True, "count": count}


class RefreshMetadataBody(BaseModel):
    """刷新元数据请求."""

    dir: str = Field("main", description="工作子目录")


@router.post("/refresh-metadata")
async def refresh_metadata(request: Request, body: RefreshMetadataBody) -> JobSubmitResponse:
    """扫描现有图片, 根据文件名 ID 重新拉取 booru 元数据(长任务)。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (RefreshMetadataBody): 刷新参数, 含工作子目录。

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度。

    Callers:
        - ``static/js/booru_filter.js:refreshMetadata``
    """
    rt = get_runtime(request)
    try:
        resolve_subdir(rt.root, body.dir)  # 仅校验目录合法
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    def run(emit: EmitFn) -> None:
        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run_refresh(rt.root / body.dir if body.dir else rt.root, emit))
        finally:
            loop.close()

    async def _run_refresh(scan_dir: Path, emit: EmitFn) -> None:
        from services.online_sources.framework.base import detect_source, get_source

        scan_dir = scan_dir.resolve()
        files: list[tuple[str, str]] = []  # (source, pid)
        for f in sorted(scan_dir.iterdir()):
            if not f.is_file() or f.suffix.lower() not in IMAGE_EXTS:
                continue
            detected = detect_source(f.stem)
            if detected:
                files.append((*detected,))

        total = len(files)
        stats: dict[str, int] = {}
        errors: list[str] = []
        emit(JobSnapshot(status="running", phase="running", total=total, done=0, pct=0,
                         message=f"共 {total} 个文件"))

        for i, (source, pid) in enumerate(files):
            pct = round(100.0 * (i + 1) / total, 1) if total else 100.0
            emit(JobSnapshot(status="running", phase="running", total=total, done=i + 1, pct=pct,
                             message=f"[{i + 1}/{total}] {source}:{pid}"))
            try:
                src = get_source(source)
                await src.fetch_meta(scan_dir, pid)
                stats[source] = stats.get(source, 0) + 1
            except Exception as e:
                errors.append(f"{source}:{pid} {e}")

        summary = ", ".join(f"{s}: {c}" for s, c in sorted(stats.items()))
        emit(JobSnapshot(status="done", phase="done", total=total, done=total, pct=100,
                         message=f"{summary}, 错误 {len(errors)}"))

    job_id = await jobs(request).submit(kind="refresh_meta", group="compute", dataset=rt.name, run=run)
    return JobSubmitResponse(ok=True, job_id=job_id)
