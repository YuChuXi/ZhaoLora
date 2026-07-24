"""Booru 挂载 API：搜索 / 预览代理 / 下载到工作目录。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
import httpx
from curl_cffi.requests.exceptions import RequestException

from services.booru_client import (
    SOURCES,
    build_search_query,
    download_post,
    file_stem,
    fetch_preview_bytes,
    fetch_sample_bytes,
    is_downloaded,
    local_rel_path,
    remove_download,
    search_posts,
)
from routes.deps import get_runtime
from services.models import (
    BooruDownloadResult,
    BooruSearchResult,
    BooruSourcesResponse,
    BooruUndownloadResult,
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
    source: str = Query(...),
    id: str = Query(..., min_length=1),
) -> Response:
    """代理预览缩略图。引用：``booru_filter.js``。"""
    source = source.strip().lower()
    if source not in SOURCES:
        raise HTTPException(400, f"不支持的源: {source}")
    try:
        data, ctype = await fetch_preview_bytes(
            source, id, proxy=_proxy(request)
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
    source: str = Query(...),
    id: str = Query(..., min_length=1),
) -> Response:
    """代理大图预览。引用：``booru_filter.js``。"""
    source = source.strip().lower()
    if source not in SOURCES:
        raise HTTPException(400, f"不支持的源: {source}")
    try:
        data, ctype = await fetch_sample_bytes(
            source, id, proxy=_proxy(request)
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
    except _NETWORK_ERRORS as e:
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
