"""去重 API：扫描重复组 / 删除选中图。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.dedupe import find_duplicate_groups, load_records
from services.scanner import scan_images
from services.work_dir import list_work_dirs, resolve_subdirs

router = APIRouter(prefix="/api/dedupe", tags=["dedupe"])


class ScanBody(BaseModel):
    dirs: list[str] = Field(default_factory=lambda: ["main"])
    dir: str | None = None
    mse_threshold: float = Field(default=1e-4, gt=0, le=1)
    similar_res_ratio: float = Field(default=1.05, ge=1.0, le=10.0)


class ApplyBody(BaseModel):
    paths: list[str]
    dirs: list[str] = Field(default_factory=lambda: ["main"])
    dir: str | None = None


def _names(dirs: list[str] | None, legacy: str | None) -> list[str]:
    if dirs:
        return list(dirs)
    if legacy:
        return [legacy]
    return ["main"]


@router.get("/dirs")
def dedupe_dirs(request: Request):
    root = request.app.state.dataset_root
    return {"root": str(root), "dirs": list_work_dirs(root), "default": "main"}


@router.post("/scan")
def scan_duplicates(request: Request, body: ScanBody):
    root = request.app.state.dataset_root
    thumbnailer = request.app.state.thumbnailer
    names = _names(body.dirs, body.dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    records = []
    for sub, work in pairs:
        part = load_records(work)
        for r in part:
            r.rel = f"{sub}/{r.rel}" if not r.rel.startswith(sub + "/") else r.rel
        records.extend(part)

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
        "dirs": [s for s, _ in pairs],
        "dir": pairs[0][0],
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
    root = request.app.state.dataset_root
    state_mgr = request.app.state.state_manager
    if not body.paths:
        raise HTTPException(400, "未选择任何图片")

    names = _names(body.dirs, body.dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    alive: set[str] = set()
    for sub, work in pairs:
        alive |= {img["path"] for img in scan_images(work, path_prefix=sub)}

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
    idx = getattr(request.app.state, "color_index", None)
    if idx is not None:
        idx.clear()

    return {"moved": moved, "paths": paths, "dirs": [s for s, _ in pairs]}
