"""去重 API：扫描重复组 / 删除选中图（数据集作用域）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs
from services.dedupe import find_duplicate_groups, load_records
from services.scanner import scan_images
from services.work_dir import list_work_dirs, resolve_subdirs

router = APIRouter(prefix="/dedupe", tags=["dedupe"])


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
    rt = get_runtime(request)
    return {"root": str(rt.root), "dirs": list_work_dirs(rt.root), "default": "main"}


@router.post("/scan")
async def scan_duplicates(request: Request, body: ScanBody):
    """
    提交去重扫描长任务。

    返回 job_id；结束事件的 result 字段含分组结果。
    引用：dedupe.js。
    """
    rt = get_runtime(request)
    root = rt.root
    thumbnailer = rt.thumbnailer
    names = _names(body.dirs, body.dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    mse = body.mse_threshold
    ratio = body.similar_res_ratio

    def run(emit) -> dict:
        emit({"status": "running", "phase": "scanning", "message": "扫描中…", "pct": 0})
        records = []
        for sub, work in pairs:
            part = load_records(work)
            for r in part:
                r.rel = f"{sub}/{r.rel}" if not r.rel.startswith(sub + "/") else r.rel
            records.extend(part)

        groups = find_duplicate_groups(
            records,
            mse_threshold=mse,
            similar_res_ratio=ratio,
        )
        for g in groups:
            for m in g["members"]:
                m["thumbnail_hash"] = thumbnailer.hash_for(m["path"])
                if not thumbnailer.exists(m["path"]):
                    thumbnailer.generate(m["path"])

        result = {
            "dirs": [s for s, _ in pairs],
            "dir": pairs[0][0],
            "image_count": len(records),
            "group_count": len(groups),
            "duplicate_image_count": sum(g["size"] for g in groups),
            "mse_threshold": mse,
            "similar_res_ratio": ratio,
            "groups": groups,
        }
        emit({
            "status": "done",
            "phase": "done",
            "message": f"完成 {len(groups)} 组",
            "pct": 100,
            "result": result,
        })
        return result

    job_id = jobs(request).submit(kind="dedupe_scan", dataset=rt.name, run=run)
    return {"ok": True, "job_id": job_id}


@router.post("/apply")
def apply_dedupe_delete(request: Request, body: ApplyBody):
    """将手选路径移入回收站（与筛选页同一套 trash）。"""
    rt = get_runtime(request)
    root = rt.root
    state_mgr = rt.state_manager
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
    idx = rt.color_index
    if idx is not None:
        idx.clear()

    return {"moved": moved, "paths": paths, "dirs": [s for s, _ in pairs]}
