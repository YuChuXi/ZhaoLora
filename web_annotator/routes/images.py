"""
图片 API —— 列表 / 原图 / 色调相似。缩略图通过 /thumb/ 静态目录直接提供。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from PIL import Image

from services.scanner import scan_images
from services.color_similarity import ColorIndex, fingerprint_file
from services.work_dir import list_work_dirs, resolve_subdir

router = APIRouter(prefix="/api", tags=["images"])


def _root(request: Request) -> Path:
    return request.app.state.dataset_root


def _services(request: Request):
    return (
        request.app.state.dataset_root,
        request.app.state.thumbnailer,
        request.app.state.state_manager,
    )


def _color_index(request: Request) -> ColorIndex:
    idx = getattr(request.app.state, "color_index", None)
    if idx is None:
        idx = ColorIndex()
        request.app.state.color_index = idx
    return idx


def _resolve_image(root: Path, trash_dir: Path, rel: str) -> Path:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise HTTPException(400, "非法路径")
    main = (root / rel).resolve()
    if str(main).startswith(str(root.resolve())) and main.is_file():
        return main
    trash = (trash_dir / rel).resolve()
    if str(trash).startswith(str(trash_dir.resolve())) and trash.is_file():
        return trash
    raise HTTPException(404, "文件不存在")


@router.get("/dirs")
def list_dirs(request: Request):
    root = _root(request)
    return {"root": str(root), "dirs": list_work_dirs(root), "default": "main"}


@router.get("/images")
def list_images(
    request: Request,
    show_trash: bool = Query(False),
    dir: str = Query("main"),
):
    """列出指定子目录图片。路径相对数据集根，如 main/a.png。"""
    root, thumbnailer, state_mgr = _services(request)
    try:
        sub, work = resolve_subdir(root, dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    state = state_mgr.get()
    selected = set(state.get("selected", {}).keys())
    trashed_state = set(state.get("trashed", {}).keys())
    prefix = sub + "/"

    images = scan_images(work, path_prefix=sub)
    result = []
    seen = set()
    for img in images:
        rel = img["path"]
        if rel in trashed_state:
            continue
        seen.add(rel)
        w, h = _image_size(root / rel)
        result.append({
            **img,
            "width": w, "height": h,
            "selected": rel in selected,
            "trashed": False,
            "thumbnail_hash": thumbnailer.hash_for(rel),
            "dir": sub,
        })

    if show_trash:
        trash_dir = state_mgr.trash_dir
        trash_work = trash_dir / sub
        if trash_work.is_dir():
            for img in scan_images(trash_work, path_prefix=sub):
                if img["path"] in seen:
                    continue
                w, h = _image_size(trash_dir / img["path"])
                result.append({
                    **img,
                    "width": w, "height": h,
                    "selected": img["path"] in selected,
                    "trashed": True,
                    "thumbnail_hash": thumbnailer.hash_for(img["path"]),
                    "dir": sub,
                })
        else:
            for rel in trashed_state:
                if not rel.startswith(prefix):
                    continue
                if rel in seen:
                    continue
                tp = trash_dir / rel
                if not tp.is_file():
                    continue
                w, h = _image_size(tp)
                result.append({
                    "path": rel,
                    "stem": Path(rel).stem,
                    "suffix": Path(rel).suffix.lower(),
                    "size": tp.stat().st_size,
                    "siblings": [],
                    "width": w, "height": h,
                    "selected": rel in selected,
                    "trashed": True,
                    "thumbnail_hash": thumbnailer.hash_for(rel),
                    "dir": sub,
                })

    return result


@router.get("/image")
def get_image(request: Request, path: str = Query(...)):
    """返回原图文件（预览用）。"""
    root, _, state_mgr = _services(request)
    file_path = _resolve_image(root, state_mgr.trash_dir, path)
    return FileResponse(file_path)


@router.get("/images/similar")
def similar_images(
    request: Request,
    path: str = Query(...),
    limit: int = Query(8, ge=1, le=24),
):
    """按色调相似度返回若干张图（不含自身，候选为同目录未删图）。"""
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
    selected = set(state.get("selected", {}).keys())
    trashed_state = set(state.get("trashed", {}).keys())
    candidates = [
        img["path"] for img in scan_images(work, path_prefix=sub)
        if img["path"] not in trashed_state
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
        return {"path": rel, "similar": []}

    main_q = root / rel
    if main_q.is_file():
        idx.put(rel, main_q.stat().st_mtime, q)

    hits = idx.similar_matrix(q, candidates, exclude=rel, limit=limit)

    out = []
    for sim_rel, dist in hits:
        w, h = _image_size(root / sim_rel)
        out.append({
            "path": sim_rel,
            "stem": Path(sim_rel).stem,
            "width": w,
            "height": h,
            "selected": sim_rel in selected,
            "trashed": sim_rel in trashed_state,
            "thumbnail_hash": thumbnailer.hash_for(sim_rel),
            "distance": round(dist, 6),
        })
    return {"path": rel, "similar": out}


_size_cache: dict[str, tuple[int, int]] = {}


def _image_size(path):
    key = str(path)
    if key not in _size_cache:
        with Image.open(path) as im:
            _size_cache[key] = im.size
    return _size_cache.get(key, (0, 0))
