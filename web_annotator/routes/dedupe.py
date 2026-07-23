"""去重 API：扫描重复组 / 删除选中图。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.dedupe import find_duplicate_groups, load_records
from services.scanner import scan_images

router = APIRouter(prefix="/api/dedupe", tags=["dedupe"])


class ScanBody(BaseModel):
    mse_threshold: float = Field(default=1e-4, gt=0, le=1)
    similar_res_ratio: float = Field(default=1.05, ge=1.0, le=10.0)


class ApplyBody(BaseModel):
    paths: list[str]


@router.post("/scan")
def scan_duplicates(request: Request, body: ScanBody):
    data_dir = request.app.state.data_dir
    thumbnailer = request.app.state.thumbnailer
    records = load_records(data_dir)
    groups = find_duplicate_groups(
        records,
        mse_threshold=body.mse_threshold,
        similar_res_ratio=body.similar_res_ratio,
    )
    for g in groups:
        for m in g["members"]:
            m["thumbnail_hash"] = thumbnailer.hash_for(m["path"])
            if not thumbnailer.exists(m["path"]):
                thumbnailer.generate(m["path"])

    return {
        "image_count": len(records),
        "group_count": len(groups),
        "duplicate_image_count": sum(g["size"] for g in groups),
        "mse_threshold": body.mse_threshold,
        "similar_res_ratio": body.similar_res_ratio,
        "groups": groups,
    }


@router.post("/apply")
def apply_dedupe_delete(request: Request, body: ApplyBody):
    """将手选路径移入回收站（与筛选页同一套 trash）。"""
    data_dir = request.app.state.data_dir
    state_mgr = request.app.state.state_manager
    if not body.paths:
        raise HTTPException(400, "未选择任何图片")

    alive = {img["path"] for img in scan_images(data_dir)}
    paths = []
    seen = set()
    for p in body.paths:
        rel = (p or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/") or rel in seen:
            continue
        if rel not in alive:
            continue
        seen.add(rel)
        paths.append(rel)

    if not paths:
        raise HTTPException(400, "没有可删除的有效路径")

    moved = state_mgr.apply_delete(paths)
    # 刷新色调索引缓存（若存在）
    idx = getattr(request.app.state, "color_index", None)
    if idx is not None:
        idx.clear()

    return {"moved": moved, "paths": paths}
