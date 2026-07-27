"""任务清单 API。"""

from __future__ import annotations

import random
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import registry
from services.dataset_runtime import DatasetRuntime
from services.models import (
    TaskCard,
    TaskCandidatesResponse,
    TaskListResponse,
    TaskOkResponse,
    SetNoteBody,
)
from services.scanner import scan_images
from services.task_store import TaskStore, validate_task_name

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class AddTaskBody(BaseModel):
    """添加任务请求体。引用：``POST /api/tasks``。"""

    name: str = Field(..., min_length=1, max_length=64)


def _store(request: Request) -> TaskStore:
    """引用：本模块各接口。"""
    return request.app.state.task_store


def _preview_hashes(rt: DatasetRuntime, limit: int = 4) -> list[str]:
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
    hashes: list[str] = []
    for img in picks:
        rel = img.path
        rt.thumbnailer.ensure(rel)
        hashes.append(rt.thumbnailer.hash_for(rel))
    return hashes


@router.get("")
def list_tasks(request: Request) -> TaskListResponse:
    """
    列出清单内全部任务及概况。

    引用：home.js。
    """
    store = _store(request)
    reg = registry(request)
    items: list[TaskCard] = []
    for entry in store.list_entries():
        name = entry.name
        task_dir = store.root / name
        image_count = 0
        previews: list[str] = []
        report_ready = False
        if (task_dir / "main").is_dir():
            rt = reg.ensure(name)
            image_count = len(scan_images(rt.main_dir, path_prefix="main"))
            previews = _preview_hashes(rt, 4)
            report_ready = (rt.root / "aesthetic_report.html").is_file()
        items.append(
            TaskCard(
                name=name,
                completed=entry.completed,
                root=str(task_dir),
                image_count=image_count,
                preview_hashes=previews,
                report_ready=report_ready,
                note=entry.note,
            )
        )
    return TaskListResponse(tasks=items, root=str(store.root))


@router.get("/candidates")
def list_candidates(request: Request) -> TaskCandidatesResponse:
    """
    任务根下尚未登记、名称合法的一级子目录，供添加任务补全。

    引用：home.js。
    """
    store = _store(request)
    registered = {e.name for e in store.list_entries()}
    names: list[str] = []
    if not store.root.is_dir():
        return TaskCandidatesResponse(candidates=names)
    for child in sorted(store.root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in registered:
            continue
        try:
            validate_task_name(child.name)
        except ValueError:
            continue
        names.append(child.name)
    return TaskCandidatesResponse(candidates=names)


@router.post("")
def add_task(request: Request, body: AddTaskBody) -> TaskOkResponse:
    """
    登记任务；必要时创建目录。

    引用：home.js。
    """
    store = _store(request)
    try:
        info = store.add(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    registry(request).ensure(info.name)
    return TaskOkResponse(
        ok=True,
        name=info.name,
        completed=info.completed,
        root=info.root,
        note=info.note,
    )


@router.post("/{name}/complete")
def complete_task(request: Request, name: str) -> TaskOkResponse:
    """引用：home.js。"""
    store = _store(request)
    try:
        info = store.set_completed(name, True)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note)


@router.post("/{name}/reopen")
def reopen_task(request: Request, name: str) -> TaskOkResponse:
    """引用：home.js。"""
    store = _store(request)
    try:
        info = store.set_completed(name, False)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note)


@router.post("/{name}/note")
def set_note(request: Request, name: str, body: SetNoteBody) -> TaskOkResponse:
    """修改任务备注。引用：home.js 双击卡片标题。"""
    store = _store(request)
    try:
        info = store.set_note(name, body.note)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note)