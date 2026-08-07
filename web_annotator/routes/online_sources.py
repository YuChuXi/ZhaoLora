"""在线来源 API: 搜索 / 预览代理 / 下载到工作目录. """

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from curl_cffi.requests.exceptions import RequestException as CffiError

from services.online_sources.batch import BatchProgress, get_progress, register_progress, run_batch
from services.online_sources import (
    download_post,
    file_stem,
    get_source,
    is_downloaded,
    list_source_names,
    local_rel_path,
    regenerate_captions,
    remove_download,
    search_posts,
)
from services.online_sources.framework import http
from routes.deps import get_runtime, jobs
from services.core.jobs import EmitFn
from services.models import (
    BatchActiveResponse,
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

router = APIRouter(prefix="/online-sources", tags=["online-sources"])

_NETWORK_ERRORS = (RuntimeError, OSError, http.NetworkError, CffiError)


class SearchBody(BaseModel):
    """Booru 搜索请求体. 引用: ``POST /online-sources/search``. """

    source: str = Field(..., description="gelbooru | danbooru")
    tags: str = ""
    page: int = Field(1, ge=1, le=10000)
    order: str = Field("", description="空 | score | id | id_asc")
    extra: str = Field("", description="额外元标签, 如 rating:g width:>=1920")
    dir: str = Field("main", description="用于标记已下载的工作子目录")


class DownloadBody(BaseModel):
    """Booru 下载 / 取消下载请求体. 引用: ``POST /online-sources/download|undownload``. """

    source: str
    id: str = Field(..., min_length=1)
    filename: str = ""
    dir: str = Field("main", description="数据集根下的工作子目录")
    overwrite: bool = False
    post: dict | None = Field(None, description="前端搜索阶段持有的帖子, 供下载直接取用")


@router.get("/sources")
def list_sources() -> BooruSourcesResponse:
    """列出可用图站与代理. 引用: ``booru_filter.js``. """
    return BooruSourcesResponse(
        sources=list_source_names(),
        proxy=os.environ.get("UPSTREAM_PROXY", "").strip(),
    )


@router.post("/search")
async def search(request: Request, body: SearchBody) -> BooruSearchResult:
    """搜索图站并标记本地下载状态. 引用: ``booru_filter.js``. """
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
        raw_posts, has_more = await search_posts(
            source, body.tags, page=body.page,
            order=order, extra=body.extra or "",
        )
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
        posts=posts,
    )


@router.get("/{source}-image")
async def proxy_image(
    source: str,
    url: str = Query(..., min_length=8, description="图床原图 URL"),
    id: str = Query(None, description="Gelbooru 帖 id, 用于合法性校验"),
    kind: str = Query("", description="thumb 或 sample"),
) -> StreamingResponse:
    """按来源代理图片(防盗链), 流式传输. 引用: ``booru_filter.js``. 
    
    图床白名单 / 防盗链头 / 超时均由各 ``SourceClient`` 自描述, 见 
    ``SourceClient.proxy_image``. 
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
    except http.NetworkError as e:
        raise HTTPException(502, f"图片拉取失败: {e}") from e
    return StreamingResponse(
        chunks, media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _cancel_on_disconnect(request: Request, task: asyncio.Task) -> None:
    """客户端断开时取消正在执行的下载任务.

    下载网络 IO 全部位于 await 点, task.cancel() 会在此抛 CancelledError;
    同步写文件块要么完整执行要么未执行, 不会留下半截目标文件.

    Args:
        request: 请求对象, 用于探测连接是否已断开.
        task: 封装的下载协程.

    Callers:
        - `routes/online_sources.py:download`
    """
    while not task.done():
        if await request.is_disconnected():
            task.cancel()
            return
        await asyncio.sleep(0.2)


@router.post("/download")
async def download(request: Request, body: DownloadBody) -> BooruDownloadResult:
    """下载帖子到工作目录; 客户端断开时取消下载. 引用: ``booru_filter.js``. """
    source = body.source.strip().lower()
    if source not in list_source_names():
        raise HTTPException(400, f"不支持的源: {body.source}")
    root = get_runtime(request).root
    try:
        name, out_dir = resolve_subdir(root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if not body.post:
        raise HTTPException(400, "缺少帖子数据 post")
    post = BooruPost.model_validate(body.post)

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
    except (OSError, http.NetworkError, CffiError) as e:
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


@router.post("/undownload")
def undownload(request: Request, body: DownloadBody) -> BooruUndownloadResult:
    """删除工作目录中该帖已下载文件. 引用: ``booru_filter.js``. """
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


@router.get("/batch/active")
def batch_active(request: Request) -> BatchActiveResponse:
    """查询本数据集未结束的批量下载任务. 引用: ``booru_filter.js`` 刷新重连. """
    rt = get_runtime(request)
    job = jobs(request).find_active(dataset=rt.name, kind="online_batch")
    if job is None:
        return BatchActiveResponse(ok=True, job_id=None, status=None)
    return BatchActiveResponse(ok=True, job_id=job.id, status=job.latest)


@router.post("/batch/start")
async def batch_start(
    request: Request, body: BatchStartBody
) -> JobSubmitResponse:
    """
    提交批量下载长任务. 

    进度见 GET /api/jobs/{id}/events；刷新后用 GET /online-sources/batch/active 重连. 
    引用: ``booru_filter.js``. 
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
        """工作线程入口. 引用: ``JobManager.submit``. """
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
    """运行中更新数量 / 连接 / 限速. 引用: ``booru_filter.js``. """
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
    """批量重生成 caption 请求. 引用: ``POST /online-sources/regenerate-captions``. """

    dir: str = Field("main", description="工作子目录")
    anima_format: bool = Field(True, description="Anima 格式")
    include_meta_tags: bool = Field(False, description="包含元数据标签")
    keep_underscores: bool = Field(False, description="保留下划线")


@router.post("/regenerate-captions")
async def regenerate_captions(request: Request, body: RegenerateCaptionsBody) -> dict:
    """从 .danbooru_meta / .gelbooru_meta 批量重生成旁路 .txt. 

    引用: ``booru_filter.js`` 批量重生成按钮. 
    """
    rt = get_runtime(request)
    try:
        _, out_dir = resolve_subdir(rt.root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    count = await regenerate_captions(
        out_dir,
        anima_format=body.anima_format,
        include_meta_tags=body.include_meta_tags,
        keep_underscores=body.keep_underscores,
    )
    return {"ok": True, "count": count}


class RefreshMetadataBody(BaseModel):
    """刷新元数据请求. 引用: ``POST /online-sources/refresh-metadata``. """

    dir: str = Field("main", description="工作子目录")


@router.post("/refresh-metadata")
async def refresh_metadata(request: Request, body: RefreshMetadataBody) -> JobSubmitResponse:
    """
    扫描现有图片, 根据文件名 ID 重新拉取 booru 元数据(长任务). 

    引用: ``booru_filter.js``. 
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