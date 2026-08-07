"""
图片 API —— 列表 / 原图 / 色调相似. 缩略图通过 /thumb/ 静态目录直接提供. 
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from routes.deps import get_runtime, resolve_path
from services.core.scanner import scan_images
from services.analysis.similarity import ColorIndex, fingerprint_file
from services.models import (
    DirsResponse,
    ImageListItem,
    SimilarItem,
    SimilarResponse,
)
from services.core.work_dir import list_work_dirs, parse_dir_names, resolve_subdir, resolve_subdirs
from services.analysis.scoring import load_score_json
from services.core.thumbnailer import Thumbnailer, image_size
from services.state.annotator import StateManager

router = APIRouter(tags=["images"])


def _services(request: Request) -> tuple[Path, Thumbnailer, StateManager]:
    """引用: 本模块列表与相似接口. """
    rt = get_runtime(request)
    return rt.root, rt.thumbnailer, rt.state_manager


def _color_index(request: Request) -> ColorIndex:
    """引用: ``similar_images``. """
    rt = get_runtime(request)
    if rt.color_index is None:
        rt.color_index = ColorIndex()
    return rt.color_index


def _resolve_image(root: Path, trash_dir: Path, rel: str) -> Path:
    """在数据集根或回收站中解析图片路径. 

    引用:
        ``get_image`` / ``similar_images``. 
    """
    main = resolve_path(root, rel)
    if main.is_file():
        return main
    trash = resolve_path(trash_dir, rel)
    if trash.is_file():
        return trash
    raise HTTPException(404, "文件不存在")


@router.get("/dirs")
def list_dirs(request: Request) -> DirsResponse:
    """列出工作子目录. 引用: 筛选页 DirPicker. """
    root = get_runtime(request).root
    return DirsResponse(dirs=list_work_dirs(root), default="main")


@router.get("/images")
def list_images(
    request: Request,
    show_trash: bool = Query(False),
    dirs: str = Query(""),
    dir: str = Query("main"),
) -> list[ImageListItem]:
    """列出指定子目录图片(dirs=main,head 或 dir=main). 路径相对数据集根. """
    root, thumbnailer, state_mgr = _services(request)
    names = parse_dir_names(dirs, dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    state = state_mgr.get()
    selected = set(state.selected.keys())
    trashed_state = set(state.trashed.keys())
    scores = load_score_json(root, ".aesthetic_scores.json")
    quality_scores = load_score_json(root, ".quality_scores.json")
    result: list[ImageListItem] = []
    seen: set[str] = set()

    for sub, work in pairs:
        prefix = sub + "/"
        for img in scan_images(work, path_prefix=sub):
            rel = img.path
            if rel in trashed_state or rel in seen:
                continue
            seen.add(rel)
            w, h = image_size(root / rel)
            result.append(
                ImageListItem(
                    **img.model_dump(),
                    width=w,
                    height=h,
                    selected=rel in selected,
                    trashed=False,
                    thumbnail_hash=thumbnailer.hash_for(rel),
                    dir=sub,
                    aesthetic=scores.get(rel),
                    quality=quality_scores.get(rel),
                )
            )

        if not show_trash:
            continue

        trash_dir = state_mgr.trash_dir
        trash_work = trash_dir / sub
        if trash_work.is_dir():
            for img in scan_images(trash_work, path_prefix=sub):
                if img.path in seen:
                    continue
                seen.add(img.path)
                tp = trash_dir / img.path
                w, h = image_size(tp)
                result.append(
                    ImageListItem(
                        **img.model_dump(),
                        width=w,
                        height=h,
                        selected=img.path in selected,
                        trashed=True,
                        thumbnail_hash=thumbnailer.hash_for(img.path),
                        dir=sub,
                        aesthetic=scores.get(img.path),
                        quality=quality_scores.get(img.path),
                    )
                )
        else:
            for rel in trashed_state:
                if not rel.startswith(prefix) or rel in seen:
                    continue
                tp = trash_dir / rel
                if not tp.is_file():
                    continue
                seen.add(rel)
                w, h = image_size(tp)
                result.append(
                    ImageListItem(
                        path=rel,
                        stem=Path(rel).stem,
                        suffix=Path(rel).suffix.lower(),
                        size=tp.stat().st_size,
                        siblings=[],
                        width=w,
                        height=h,
                        selected=rel in selected,
                        trashed=True,
                        thumbnail_hash=thumbnailer.hash_for(rel),
                        dir=sub,
                        aesthetic=scores.get(rel),
                        quality=quality_scores.get(rel),
                    )
                )

    return result


@router.get("/image")
def get_image(request: Request, path: str = Query(...)) -> FileResponse:
    """返回原图文件(预览用). """
    root, _, state_mgr = _services(request)
    file_path = _resolve_image(root, state_mgr.trash_dir, path)
    return FileResponse(file_path)


@router.get("/images/similar")
async def similar_images(
    request: Request,
    path: str = Query(...),
    limit: int = Query(8, ge=1, le=24),
) -> SimilarResponse:
    """按色调相似度返回若干张图(不含自身, 候选为同目录未删图). """
    root, thumbnailer, state_mgr = _services(request)
    rel = path.replace("\\", "/").lstrip("/")
    query_file = _resolve_image(root, state_mgr.trash_dir, rel)

    parts = rel.split("/")
    sub = parts[0] if len(parts) > 1 else "main"
    try:
        _, work = resolve_subdir(root, sub)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    state = state_mgr.get()
    selected = set(state.selected.keys())
    trashed_state = set(state.trashed.keys())
    candidates = [
        img.path for img in scan_images(work, path_prefix=sub)
        if img.path not in trashed_state
    ]

    idx = _color_index(request)
    idx.set_cache_dir(thumbnailer.thumb_dir)

    thumb_map = {c: thumbnailer.get_path(c) for c in candidates}
    idx.ensure_many(root, candidates, thumb_map)

    q_thumb = thumbnailer.get_path(rel) if (root / rel).is_file() else None
    if q_thumb is not None and q_thumb.is_file():
        q = fingerprint_file(q_thumb)
    else:
        q = fingerprint_file(query_file)
    if q is None:
        return SimilarResponse(path=rel, similar=[])

    main_q = root / rel
    if main_q.is_file():
        idx.put(rel, main_q.stat().st_mtime, q)

    hits = idx.similar_matrix(q, candidates, exclude=rel, limit=limit)

    out: list[SimilarItem] = []
    for sim_rel, dist in hits:
        await thumbnailer.ensure_async(sim_rel)
        w, h = image_size(root / sim_rel)
        out.append(
            SimilarItem(
                path=sim_rel,
                stem=Path(sim_rel).stem,
                width=w,
                height=h,
                selected=sim_rel in selected,
                trashed=sim_rel in trashed_state,
                thumbnail_hash=thumbnailer.hash_for(sim_rel),
                distance=round(dist, 6),
            )
        )
    return SimilarResponse(path=rel, similar=out)
