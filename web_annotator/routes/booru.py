"""Booru 挂载 API：搜索 / 预览代理 / 下载到工作目录。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
import httpx
from curl_cffi.requests.exceptions import RequestException

from services.booru_batch import BooruBatchProgress, get_progress, register_progress, run_booru_batch
from services.booru_client import (
    SOURCES,
    build_search_query,
    download_post,
    file_stem,
    fetch_gelbooru_bytes,
    is_downloaded,
    local_rel_path,
    regenerate_captions,
    remove_download,
    search_posts,
)
from routes.deps import get_runtime, jobs
from services.jobs import EmitFn
from services.models import (
    BooruBatchActiveResponse,
    BooruBatchParamsBody,
    BooruBatchStartBody,
    BooruDownloadResult,
    BooruSearchResult,
    BooruSourcesResponse,
    BooruUndownloadResult,
    JobSubmitResponse,
)
from services.work_dir import resolve_subdir

router = APIRouter(prefix="/booru", tags=["booru"])

_NETWORK_ERRORS = (RuntimeError, OSError, httpx.HTTPError, RequestException)


def _proxy(request: Request) -> str | None:
    """读取进程级 Booru 代理配置。引用：本模块各网络接口。"""
    return getattr(request.app.state, "booru_proxy", None)


class SearchBody(BaseModel):
    """Booru 搜索请求体。引用：``POST /booru/search``。"""

    source: str = Field(..., description="gelbooru | danbooru")
    tags: str = ""
    page: int = Field(1, ge=1, le=10000)
    limit: int = Field(40, ge=1, le=100)
    order: str = Field("", description="空 | score | id | id_asc")
    extra: str = Field("", description="额外元标签，如 rating:g width:>=1920")
    dir: str = Field("main", description="用于标记已下载的工作子目录")


class DownloadBody(BaseModel):
    """Booru 下载 / 取消下载请求体。引用：``POST /booru/download|undownload``。"""

    source: str
    id: str = Field(..., min_length=1)
    dir: str = Field("main", description="数据集根下的工作子目录")
    overwrite: bool = False


@router.get("/sources")
def booru_sources(request: Request) -> BooruSourcesResponse:
    """列出可用图站与代理。引用：``booru_filter.js``。"""
    return BooruSourcesResponse(
        sources=sorted(SOURCES),
        proxy=_proxy(request) or "",
    )


@router.post("/search")
async def booru_search(request: Request, body: SearchBody) -> BooruSearchResult:
    """搜索图站并标记本地下载状态。引用：``booru_filter.js``。"""
    source = body.source.strip().lower()
    if source not in SOURCES:
        raise HTTPException(400, f"不支持的源: {body.source}")
    order = (body.order or "").strip().lower()
    if order and order not in ("score", "id", "id_asc"):
        raise HTTPException(400, f"不支持的排序: {body.order}")
    query = build_search_query(
        source, body.tags, order=order, extra=body.extra or ""
    )
    root = get_runtime(request).root
    try:
        _, out_dir = resolve_subdir(root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        posts, has_more = await search_posts(
            source,
            body.tags,
            page=body.page,
            limit=body.limit,
            order=order,
            extra=body.extra or "",
            proxy=_proxy(request),
            danbooru_login=request.app.state.danbooru_login,
            danbooru_api_key=request.app.state.danbooru_api_key,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except _NETWORK_ERRORS as e:
        raise HTTPException(502, f"搜索失败: {e}") from e
    for p in posts:
        p.downloaded = is_downloaded(out_dir, source, p.id)
        p.stem = file_stem(source, p.id)
    return BooruSearchResult(
        source=source,
        tags=body.tags.strip(),
        extra=(body.extra or "").strip(),
        order=order,
        query=query,
        dir=body.dir,
        page=body.page,
        limit=body.limit,
        count=len(posts),
        has_more=has_more,
        posts=posts,
    )


@router.get("/thumb")
async def booru_thumb(
    request: Request,
    id: str = Query(..., min_length=1),
    url: str = Query(..., min_length=8, description="搜索结果 preview_url"),
) -> Response:
    """代理 Gelbooru 缩略图。引用：``booru_filter.js``。"""
    try:
        data, ctype = await fetch_gelbooru_bytes(
            "thumb", id, url=url, proxy=_proxy(request)
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except _NETWORK_ERRORS as e:
        raise HTTPException(502, f"预览失败: {e}") from e
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/sample")
async def booru_sample(
    request: Request,
    id: str = Query(..., min_length=1),
    url: str = Query(..., min_length=8, description="搜索结果 sample_url"),
) -> Response:
    """代理 Gelbooru 大图。引用：``booru_filter.js``。"""
    try:
        data, ctype = await fetch_gelbooru_bytes(
            "sample", id, url=url, proxy=_proxy(request)
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except _NETWORK_ERRORS as e:
        raise HTTPException(502, f"大图失败: {e}") from e
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/download")
async def booru_download(request: Request, body: DownloadBody) -> BooruDownloadResult:
    """下载帖子到工作目录。引用：``booru_filter.js``。"""
    source = body.source.strip().lower()
    if source not in SOURCES:
        raise HTTPException(400, f"不支持的源: {body.source}")
    root = get_runtime(request).root
    try:
        name, out_dir = resolve_subdir(root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        result = await download_post(
            source,
            body.id.strip(),
            out_dir,
            proxy=_proxy(request),
            overwrite=body.overwrite,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        msg = str(e)
        code = 429 if "429" in msg else 502
        raise HTTPException(code, f"下载失败: {e}") from e
    except (OSError, httpx.HTTPError, RequestException) as e:
        raise HTTPException(502, f"下载失败: {e}") from e
    rel = local_rel_path(root, result.path)
    thumb = get_runtime(request).thumbnailer
    thumb.ensure(rel)
    return result.model_copy(
        update={
            "dir": name,
            "rel_path": rel,
            "thumbnail_hash": thumb.hash_for(rel),
        }
    )


@router.post("/undownload")
def booru_undownload(request: Request, body: DownloadBody) -> BooruUndownloadResult:
    """删除工作目录中该帖已下载文件。引用：``booru_filter.js``。"""
    source = body.source.strip().lower()
    if source not in SOURCES:
        raise HTTPException(400, f"不支持的源: {body.source}")
    root = get_runtime(request).root
    try:
        name, out_dir = resolve_subdir(root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        result = remove_download(source, body.id.strip(), out_dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    thumb = get_runtime(request).thumbnailer
    stems = [file_stem(source, body.id.strip()), body.id.strip()]
    for stem in stems:
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            rel = f"{name}/{stem}{ext}"
            path = thumb.get_path(rel)
            if path.is_file():
                path.unlink()

    return result.model_copy(update={"dir": name})


@router.get("/batch/active")
def booru_batch_active(request: Request) -> BooruBatchActiveResponse:
    """查询本数据集未结束的批量下载任务。引用：``booru_filter.js`` 刷新重连。"""
    rt = get_runtime(request)
    job = jobs(request).find_active(dataset=rt.name, kind="booru_batch")
    if job is None:
        return BooruBatchActiveResponse(ok=True, job_id=None, status=None)
    return BooruBatchActiveResponse(ok=True, job_id=job.id, status=job.latest)


@router.post("/batch/start")
async def booru_batch_start(
    request: Request, body: BooruBatchStartBody
) -> JobSubmitResponse:
    """
    提交批量下载长任务。

    进度见 GET /api/jobs/{id}/events；刷新后用 GET /booru/batch/active 重连。
    引用：``booru_filter.js``。
    """
    source = body.source.strip().lower()
    if source not in SOURCES:
        raise HTTPException(400, f"不支持的源: {body.source}")
    order = (body.order or "").strip().lower()
    if order and order not in ("score", "id", "id_asc"):
        raise HTTPException(400, f"不支持的排序: {body.order}")

    rt = get_runtime(request)
    mgr = jobs(request)
    existing = mgr.find_active(dataset=rt.name, kind="booru_batch")
    if existing is not None:
        raise HTTPException(409, "批量下载已在进行中")

    try:
        _, out_dir = resolve_subdir(rt.root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    prog = BooruBatchProgress(
        limit=body.limit,
        conn=body.conn,
        rate_rps=body.rate_rps,
        dataset=rt.name,
    )
    proxy = _proxy(request)
    thumb = rt.thumbnailer
    tags = body.tags
    extra = body.extra or ""
    start_page = body.page
    root = rt.root
    anima_format = bool(body.anima_format)
    include_meta_tags = bool(body.include_meta_tags)
    keep_underscores = bool(body.keep_underscores)
    danbooru_login = request.app.state.danbooru_login
    danbooru_api_key = request.app.state.danbooru_api_key
    job_box: dict[str, str] = {"id": ""}

    def run(emit: EmitFn) -> None:
        """工作线程入口。引用：``JobManager.submit``。"""
        run_booru_batch(
            job_id=job_box["id"],
            source=source,
            tags=tags,
            extra=extra,
            order=order,
            out_dir=out_dir,
            root=root,
            start_page=start_page,
            proxy=proxy,
            progress=prog,
            emit=emit,
            thumbnailer=thumb,
            anima_format=anima_format,
            include_meta_tags=include_meta_tags,
            keep_underscores=keep_underscores,
            danbooru_login=danbooru_login,
            danbooru_api_key=danbooru_api_key,
        )

    job_id = mgr.submit(
        kind="booru_batch",
        group="network",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    job_box["id"] = job_id
    register_progress(job_id, prog)
    return JobSubmitResponse(ok=True, job_id=job_id)


@router.post("/batch/params")
def booru_batch_params(request: Request, body: BooruBatchParamsBody) -> JobSubmitResponse:
    """运行中更新数量 / 连接 / 限速。引用：``booru_filter.js``。"""
    rt = get_runtime(request)
    job = jobs(request).find_active(dataset=rt.name, kind="booru_batch")
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
    """批量重生成 caption 请求。引用：``POST /booru/regenerate-captions``。"""

    dir: str = Field("main", description="工作子目录")
    anima_format: bool = Field(True, description="Anima 格式")
    include_meta_tags: bool = Field(False, description="包含元数据标签")
    keep_underscores: bool = Field(False, description="保留下划线")


@router.post("/regenerate-captions")
def booru_regenerate_captions(request: Request, body: RegenerateCaptionsBody) -> dict:
    """从 .danbooru_meta / .gelbooru_meta 批量重生成旁路 .txt。

    引用：``booru_filter.js`` 批量重生成按钮。
    """
    from routes.booru import resolve_subdir

    rt = get_runtime(request)
    try:
        _, out_dir = resolve_subdir(rt.root, body.dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    count = regenerate_captions(
        out_dir,
        anima_format=body.anima_format,
        include_meta_tags=body.include_meta_tags,
        keep_underscores=body.keep_underscores,
    )
    return {"ok": True, "count": count}