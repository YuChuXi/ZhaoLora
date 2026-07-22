"""
图片 API —— 列表。缩略图通过 /thumb/ 静态目录直接提供。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from PIL import Image

from services.scanner import scan_images

router = APIRouter(prefix="/api", tags=["images"])


def _services(request: Request):
    return request.app.state.data_dir, request.app.state.thumbnailer, request.app.state.state_manager


@router.get("/images")
def list_images(request: Request, show_trash: bool = Query(False)):
    """列出所有图片及元数据。show_trash=1 时同时返回回收站中的图片。"""
    data_dir, thumbnailer, state_mgr = _services(request)

    state = state_mgr.get()
    selected = set(state.get("selected", {}).keys())
    trashed_state = set(state.get("trashed", {}).keys())

    # 主目录中的活跃图片
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

    # 回收站中的图片
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


_size_cache: dict[str, tuple[int, int]] = {}

def _image_size(path):
    key = str(path)
    if key not in _size_cache:
        with Image.open(path) as im:
            _size_cache[key] = im.size
    return _size_cache.get(key, (0, 0))
