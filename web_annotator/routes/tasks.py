"""任务清单 API. """

from __future__ import annotations

import asyncio
import random
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from routes.deps import jobs, registry
from services.dataset.runtime import DatasetRuntime
from services.models import (
    ArchivedListResponse,
    JobSubmitResponse,
    ScannedImage,
    TaskCard,
    TaskCandidatesResponse,
    TaskListResponse,
    TaskOkResponse,
    SetNoteBody,
    SetTriggerWordBody,
)
from services.core.scanner import scan_images
from services.core.work_dir import total_image_count
from services.state.archiver import (
    PACKED_DIR,
    archive_task,
    find_archive,
    list_archives,
    restore_task,
)
from services.state.tasks import TaskStore, validate_task_name

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
SortField = Literal["sort_order", "name", "image_count", "created"]
SortOrder = Literal["asc", "desc"]


class AddTaskBody(BaseModel):
    """添加任务请求体."""

    name: str = Field(..., min_length=1, max_length=64)


def _store(request: Request) -> TaskStore:
    """返回应用级任务存储实例。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。

    Returns:
        TaskStore: 任务存储实例。
    """
    return request.app.state.task_store


async def _preview_hashes(
    rt: DatasetRuntime, images: list[ScannedImage], limit: int = 4
) -> list[str]:
    """从 images 随机取最多 limit 张缩略图 hash, 缺失缩略图并发生成。

    Args:
        rt (DatasetRuntime): 数据集运行时(用于 thumbnailer)。
        images (list[ScannedImage]): 已扫描的图片列表(由调用方传入, 避免重复扫描)。
        limit (int): 最多选取的图片数。

    Returns:
        list[str]: 缩略图 hash 列表。

    Callers:
        - ``routes/tasks:list_tasks``
    """
    if not images:
        return []
    picks = images if len(images) <= limit else random.sample(images, limit)
    missing = [img.path for img in picks if not rt.thumbnailer.exists(img.path)]
    if missing:
        await asyncio.gather(
            *(rt.thumbnailer.ensure_async(rel) for rel in missing)
        )
    return [rt.thumbnailer.hash_for(img.path) for img in picks]


@router.get("")
async def list_tasks(
    request: Request,
    sort_by: SortField = Query("sort_order", description="排序字段"),
    sort_order: SortOrder = Query("asc", description="排序方向"),
) -> TaskListResponse:
    """列出清单内全部任务及概况。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        sort_by (SortField): 排序字段, ``sort_order`` / ``name`` / ``image_count`` / ``created``。
        sort_order (SortOrder): 排序方向, ``asc`` 或 ``desc``。

    Returns:
        TaskListResponse: 含任务卡片列表, 含缩略图预览、统计、备注等。

    Callers:
        - ``static/js/index.js:loadTasks``
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
            images = scan_images(rt.main_dir, path_prefix="main")
            previews = await _preview_hashes(rt, images, 4)
            image_count = total_image_count(rt.root)
            report_ready = (rt.root / "aesthetic_report.html").is_file()
        items.append(
            TaskCard(
                name=name,
                completed=entry.completed,
                image_count=image_count,
                preview_hashes=previews,
                report_ready=report_ready,
                note=entry.note,
                trigger_word=entry.trigger_word,
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
    """任务根下尚未登记、名称合法的一级子目录, 供添加任务补全。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。

    Returns:
        TaskCandidatesResponse: 含候选目录名列表。

    Callers:
        - ``static/js/index.js:loadCandidates``
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
    """登记任务；必要时创建目录。

    须为协程: ``ensure`` 会经 JobManager.submit 提交后台缩略图任务, 而后者
    依赖运行中的事件循环, 同步路由在线程池中执行会触发 no running event loop。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (AddTaskBody): 含任务名。

    Returns:
        TaskOkResponse: 含任务名、完成状态、备注、触发词。

    Callers:
        - ``static/js/index.js:taskAddFormSubmit``
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
        trigger_word=info.trigger_word,
    )


@router.post("/{name}/complete")
def complete_task(request: Request, name: str) -> TaskOkResponse:
    """标记任务为已完成。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        name (str): 任务名。

    Returns:
        TaskOkResponse: 含任务名与最新完成状态。

    Callers:
        - ``static/js/index.js:buildCard``
    """
    store = _store(request)
    try:
        info = store.set_completed(name, True)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note, trigger_word=info.trigger_word)


@router.post("/{name}/reopen")
def reopen_task(request: Request, name: str) -> TaskOkResponse:
    """重新打开已完成任务。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        name (str): 任务名。

    Returns:
        TaskOkResponse: 含任务名与最新完成状态。

    Callers:
        - ``static/js/index.js:buildCard``
    """
    store = _store(request)
    try:
        info = store.set_completed(name, False)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note, trigger_word=info.trigger_word)


@router.post("/{name}/note")
def set_note(request: Request, name: str, body: SetNoteBody) -> TaskOkResponse:
    """修改任务备注。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        name (str): 任务名。
        body (SetNoteBody): 含新备注文本。

    Returns:
        TaskOkResponse: 含任务名与最新备注。

    Callers:
        - ``static/js/index.js:startEditNote``
    """
    store = _store(request)
    try:
        info = store.set_note(name, body.note)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note, trigger_word=info.trigger_word)


@router.post("/{name}/trigger-word")
def set_trigger_word(request: Request, name: str, body: SetTriggerWordBody) -> TaskOkResponse:
    """修改任务触发词(写入就绪 TOML 的 subset class_tokens)。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        name (str): 任务名。
        body (SetTriggerWordBody): 含触发词文本。

    Returns:
        TaskOkResponse: 含任务名与最新触发词。

    Callers:
        - ``static/js/index.js:buildCard``
        - ``static/js/tags.js:setTrigger``
    """
    store = _store(request)
    try:
        info = store.set_trigger_word(name, body.trigger_word)
    except KeyError:
        raise HTTPException(404, f"未知任务: {name}") from None
    return TaskOkResponse(ok=True, name=info.name, completed=info.completed, note=info.note, trigger_word=info.trigger_word)


class ReorderBody(BaseModel):
    """重排请求体."""

    order: list[str] = Field(..., min_length=1)


@router.post("/reorder")
def reorder_tasks(request: Request, body: ReorderBody) -> dict:
    """按指定名称顺序重设所有任务的 sort_order。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (ReorderBody): 含按目标顺序排列的任务名列表。

    Returns:
        dict: ``{"ok": True}``, 表示重排成功。

    Callers:
        - ``static/js/index.js:setupDragDrop``
    """
    store = _store(request)
    try:
        store.reorder(body.order)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


# ── 归档 ──


@router.post("/{name}/archive")
async def archive_task_route(request: Request, name: str) -> JobSubmitResponse:
    """归档已完成任务: 7z 极限压缩 → 校验 → 删除目录。

    须为协程: 内部提交 JobManager 任务, 后者依赖事件循环。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        name (str): 任务名。

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度。

    Callers:
        - ``static/js/index.js:archiveTask``
    """
    store = _store(request)
    entries = {e.name: e for e in store.list_entries()}
    if name not in entries:
        raise HTTPException(404, f"未知任务: {name}")
    if not entries[name].completed:
        raise HTTPException(400, "任务未完成, 无法归档")
    task_dir = store.root / name
    if not (task_dir / "main").is_dir():
        raise HTTPException(400, "任务目录不存在")
    reg = registry(request)
    rt = await reg.ensure(name)

    def run(emit):
        """归档执行体(工作线程)。

        Args:
            emit (EmitFn): 任务事件推送回调。

        Returns:
            object: 归档元数据。

        Callers:
            - ``services.core.jobs:JobManager._execute``
        """
        meta = archive_task(task_dir, name, store.root, rt.thumbnailer, emit)
        store.remove(name)
        return meta

    job_id = await jobs(request).submit(
        kind="archive", group=f"archive_{name}", dataset=name, run=run,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)


archive_router = APIRouter(prefix="/api/archived", tags=["archive"])


@archive_router.get("")
def list_archived(request: Request) -> ArchivedListResponse:
    """列出所有已归档任务。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。

    Returns:
        ArchivedListResponse: 含归档元数据列表。

    Callers:
        - ``static/js/index.js:loadArchived``
    """
    store = _store(request)
    return ArchivedListResponse(archives=list_archives(store.root))


@archive_router.post("/{base}/restore")
async def restore_task_route(request: Request, base: str) -> JobSubmitResponse:
    """解压归档恢复任务目录。

    须为协程: 内部提交 JobManager 任务, 后者依赖事件循环。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        base (str): 归档基本名(name-timestamp)。

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度。

    Callers:
        - ``static/js/index.js:restoreArchive``
    """
    store = _store(request)
    meta = find_archive(store.root, base)
    if meta is None:
        raise HTTPException(404, f"归档不存在: {base}")
    archive_path = store.root / PACKED_DIR / meta.archive_file
    if not archive_path.is_file():
        raise HTTPException(404, "归档文件缺失")

    def run(emit):
        """解压执行体(工作线程)。

        Args:
            emit (EmitFn): 任务事件推送回调。

        Returns:
            None: 无返回值, 仅推送进度并恢复任务。

        Callers:
            - ``services.core.jobs:JobManager._execute``
        """
        restore_task(archive_path, store.root, emit)
        info = store.add(meta.name)
        store.set_completed(info.name, True)

    job_id = await jobs(request).submit(
        kind="archive", group=f"restore_{base}", dataset=meta.name, run=run,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)


@archive_router.get("/{base}/preview/{filename}")
def serve_archive_preview(request: Request, base: str, filename: str) -> FileResponse:
    """提供归档预览缩略图。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        base (str): 归档基本名(name-timestamp)。
        filename (str): 缩略图文件名({hash}.avif)。

    Returns:
        FileResponse: AVIF 缩略图文件响应。

    Callers:
        - ``static/js/index.js:buildArchivedCard``
    """
    store = _store(request)
    f = store.root / PACKED_DIR / f"{base}.previews" / filename
    if not f.is_file():
        raise HTTPException(404, "预览不存在")
    return FileResponse(f, media_type="image/avif")
