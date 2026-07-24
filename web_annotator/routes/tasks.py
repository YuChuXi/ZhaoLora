"""任务清单 API。"""

from __future__ import annotations

import random
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import registry
from services.scanner import scan_images
from services.task_store import TaskStore

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class AddTaskBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


def _store(request: Request) -> TaskStore:
    return request.app.state.task_store


def _preview_hashes(rt, limit: int = 4) -> list[str]:
    """
    从 main 随机取最多 limit 张已有或可生成的缩略图 hash。

    引用：GET /api/tasks。
    """
    main = rt.main_dir
    if not main.is_dir():
        return []
    images = scan_images(main, path_prefix="main")
    if not images:
        return []
    picks = images if len(images) <= limit else random.sample(images, limit)
    hashes = []
    for img in picks:
        rel = img["path"]
        rt.thumbnailer.ensure(rel)
        hashes.append(rt.thumbnailer.hash_for(rel))
    return hashes


@router.get("")
def list_tasks(request: Request):
    """
    列出清单内全部任务及概况。

    引用：tasks_home.js。
    """
    store = _store(request)
    reg = registry(request)
    items = []
    for entry in store.list_entries():
        name = entry["name"]
        task_dir = store.root / name
        image_count = 0
        previews: list[str] = []
        report_ready = False
        if (task_dir / "main").is_dir():
            rt = reg.ensure(name)
            image_count = len(scan_images(rt.main_dir, path_prefix="main"))
            previews = _preview_hashes(rt, 4)
            report_ready = (rt.root / "aesthetic_report.html").is_file()
        items.append({
            "name": name,
            "completed": entry["completed"],
            "root": str(task_dir),
            "image_count": image_count,
            "preview_hashes": previews,
            "report_ready": report_ready,
        })
    return {"tasks": items, "root": str(store.root)}


@router.post("")
def add_task(request: Request, body: AddTaskBody):
    """
    登记任务；必要时创建目录。

    引用：tasks_home.js。
    """
    store = _store(request)
    try:
        info = store.add(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    registry(request).ensure(info["name"])
    return {"ok": True, **info}


@router.post("/{name}/complete")
def complete_task(request: Request, name: str):
    """引用：tasks_home.js。"""
    store = _store(request)
    try:
        info = store.set_completed(name, True)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return {"ok": True, **info}


@router.post("/{name}/reopen")
def reopen_task(request: Request, name: str):
    """引用：tasks_home.js。"""
    store = _store(request)
    try:
        info = store.set_completed(name, False)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return {"ok": True, **info}
