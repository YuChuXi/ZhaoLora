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

router = APIRouter(prefix="/api", tags=["images"])


def _services(request: Request):
    return request.app.state.data_dir, request.app.state.thumbnailer, request.app.state.state_manager


def _color_index(request: Request) -> ColorIndex:
    idx = getattr(request.app.state, "color_index", None)
    if idx is None:
        idx = ColorIndex()
        request.app.state.color_index = idx
    return idx


def _resolve_image(data_dir: Path, trash_dir: Path, rel: str) -> Path:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise HTTPException(400, "非法路径")
    main = (data_dir / rel).resolve()
    if str(main).startswith(str(data_dir.resolve())) and main.is_file():
        return main
    trash = (trash_dir / rel).resolve()
    if str(trash).startswith(str(trash_dir.resolve())) and trash.is_file():
        return trash
    raise HTTPException(404, "文件不存在")


@router.get("/images")
def list_images(request: Request, show_trash: bool = Query(False)):
    """列出所有图片及元数据。show_trash=1 时同时返回回收站中的图片。"""
    data_dir, thumbnailer, state_mgr = _services(request)

    state = state_mgr.get()
    selected = set(state.get("selected", {}).keys())
    trashed_state = set(state.get("trashed", {}).keys())

    images = scan_images(data_dir)
    result = []
    seen = set()
    for img in images:
        rel = img["path"]
        seen.add(rel)
        w, h = _image_size(data_dir / rel)
        result.append({
            **img,
            "width": w, "height": h,
            "selected": rel in selected,
            "trashed": rel in trashed_state,
            "thumbnail_hash": thumbnailer.hash_for(rel),
        })

    if show_trash:
        trash_dir = state_mgr.trash_dir
        if trash_dir.exists():
            trashed_images = scan_images(trash_dir)
            for img in trashed_images:
                if img["path"] not in seen:
                    w, h = _image_size(trash_dir / img["path"])
                    result.append({
                        **img,
                        "width": w, "height": h,
                        "selected": img["path"] in selected,
                        "trashed": True,
                        "thumbnail_hash": thumbnailer.hash_for(img["path"]),
                    })

    return result


@router.get("/image")
def get_image(request: Request, path: str = Query(...)):
    """返回原图文件（预览用）。"""
    data_dir, _, state_mgr = _services(request)
    file_path = _resolve_image(data_dir, state_mgr.trash_dir, path)
    return FileResponse(file_path)


@router.get("/images/similar")
def similar_images(
    request: Request,
    path: str = Query(...),
    limit: int = Query(8, ge=1, le=24),
):
    """按色调相似度返回若干张图（不含自身，候选为主目录未删图）。"""
    data_dir, thumbnailer, state_mgr = _services(request)
    rel = path.replace("\\", "/").lstrip("/")
    query_file = _resolve_image(data_dir, state_mgr.trash_dir, rel)

    state = state_mgr.get()
    selected = set(state.get("selected", {}).keys())
    trashed_state = set(state.get("trashed", {}).keys())
    candidates = [img["path"] for img in scan_images(data_dir)]

    idx = _color_index(request)
    idx.set_cache_dir(thumbnailer.thumb_dir)

    thumb_map = {c: thumbnailer.get_path(c) for c in candidates}
    idx.ensure_many(data_dir, candidates, thumb_map)

    # 查询指纹：优先缩略图
    q_thumb = thumbnailer.get_path(rel) if (data_dir / rel).is_file() else None
    if q_thumb is not None and q_thumb.is_file():
        q = fingerprint_file(q_thumb)
    else:
        q = fingerprint_file(query_file)
    if q is None:
        return {"path": rel, "similar": []}

    main_q = data_dir / rel
    if main_q.is_file():
        idx.put(rel, main_q.stat().st_mtime, q)

    hits = idx.similar_matrix(q, candidates, exclude=rel, limit=limit)

    out = []
    for sim_rel, dist in hits:
        w, h = _image_size(data_dir / sim_rel)
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
