"""任务清单 API. """

from __future__ import annotations

import random
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from routes.deps import registry
from services.dataset.runtime import DatasetRuntime
from services.models import (
    TaskCard,
    TaskCandidatesResponse,
    TaskListResponse,
    TaskOkResponse,
    SetNoteBody,
)
from services.core.scanner import scan_images
from services.state.tasks import TaskStore, validate_task_name

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
SortField = Literal["sort_order", "name", "image_count", "created"]
SortOrder = Literal["asc", "desc"]


class AddTaskBody(BaseModel):
    """添加任务请求体. 引用: ``POST /api/tasks``. """

    name: str = Field(..., min_length=1, max_length=64)


def _store(request: Request) -> TaskStore:
    """引用: 本模块各接口. """
    return request.app.state.task_store


async def _preview_hashes(rt: DatasetRuntime, limit: int = 4) -> list[str]:
    """
    从 main 随机取最多 limit 张已有或可生成的缩略图 hash. 

    引用: GET /api/tasks. 
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
        await rt.thumbnailer.ensure_async(rel)
        hashes.append(rt.thumbnailer.hash_for(rel))
    return hashes


@router.get("")
async def list_tasks(
    request: Request,
    sort_by: SortField = Query("sort_order", description="排序字段"),
    sort_order: SortOrder = Query("asc", description="排序方向"),
) -> TaskListResponse:
    """
    列出清单内全部任务及概况. 

    引用: home.js. 
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
            rt = await reg.ensure(name)
            image_count = len(scan_images(rt.main_dir, path_prefix="main"))
            previews = await _preview_hashes(rt, 4)
            report_ready = (rt.root / "aesthetic_report.html").is_file()
        items.append(
            TaskCard(
                name=name,
                completed=entry.completed,
                image_count=image_count,
                preview_hashes=previews,
                report_ready=report_ready,
                note=entry.note,
                created=entry.created,
                sort_order=entry.sort_order,
            )
        )

    reverse = sort_order == "desc"
    if sort_by == "sort_order":
        items.sort(key=lambda t: t.sort_order, reverse=reverse)
    elif sort_by == "name":
        items.sort(key=lambda t: t.name.lower(), reverse=reverse)
    elif sort_by == "image_count":
        items.sort(key=lambda t: t.image_count, reverse=reverse)
    elif sort_by == "created":
        items.sort(key=lambda t: t.created, reverse=reverse)

    return TaskListResponse(tasks=items)


@router.get("/candidates")
def list_candidates(request: Request) -> TaskCandidatesResponse:
    """
    任务根下尚未登记、名称合法的一级子目录, 供添加任务补全. 

    引用: home.js. 
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
async def add_task(request: Request, body: AddTaskBody) -> TaskOkResponse:
    """
    登记任务；必要时创建目录. 

    须为协程: ``ensure`` 会经 JobManager.submit 提交后台缩略图任务, 而后者
    依赖运行中的事件循环, 同步路由在线程池中执行会触发 no running event loop.

    引用: home.js. 
    """
    store = _store(request)
    try:
        info = store.add(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await registry(request).ensure(info.name)
    return TaskOkResponse(
        ok=True,
        name=info.name,
        completed=info.completed,
        note=info.note,
    )


@router.post("/{name}/complete")
def complete_task(request: Request, name: str) -> TaskOkResponse:
    """引用: home.js. """
    store = _store(request)
    try:
        info = store.set_completed(name, True)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note)


@router.post("/{name}/reopen")
def reopen_task(request: Request, name: str) -> TaskOkResponse:
    """引用: home.js. """
    store = _store(request)
    try:
        info = store.set_completed(name, False)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note)


@router.post("/{name}/note")
def set_note(request: Request, name: str, body: SetNoteBody) -> TaskOkResponse:
    """修改任务备注. 引用: home.js 双击卡片标题. """
    store = _store(request)
    try:
        info = store.set_note(name, body.note)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note)


class ReorderBody(BaseModel):
    """重排请求体. 引用: ``POST /api/tasks/reorder``. """

    order: list[str] = Field(..., min_length=1)


@router.post("/reorder")
def reorder_tasks(request: Request, body: ReorderBody) -> dict:
    """
    按指定名称顺序重设所有任务的 sort_order. 

    引用: home.js 拖拽回调. 
    """
    store = _store(request)
    try:
        store.reorder(body.order)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}