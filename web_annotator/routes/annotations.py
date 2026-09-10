"""标注操作 API: 选中、全选、删除到回收站、恢复. """

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, resolve_path
from services.models import (
    AnnotatorStateResponse,
    CountResponse,
    SelectAllResponse,
    ToggleResponse,
)
from services.core.scanner import scan_images
from services.state.annotator import StateManager
from services.core.work_dir import parse_dir_names, resolve_subdirs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["annotations"])


class ToggleBody(BaseModel):
    """单图选中切换."""

    path: str = Field(..., min_length=1)


class SelectAllBody(BaseModel):
    """全选指定工作子目录."""

    dirs: list[str] | None = None
    dir: str | None = None
    show_trash: bool = False


class RestoreBody(BaseModel):
    """从回收站恢复."""

    paths: list[str] = Field(default_factory=list)


def _root(request: Request) -> Path:
    """返回当前请求绑定的数据集根目录。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。

    Returns:
        Path: 数据集根目录的绝对路径。
    Callers:
        - ``routes/annotations.py:apply_delete``
        - ``routes/annotations.py:get_state``
        - ``routes/annotations.py:restore``
        - ``routes/annotations.py:select_all``
        - ``routes/annotations.py:toggle``
    """
    return get_runtime(request).root


def _state(request: Request) -> StateManager:
    """返回当前请求绑定的标注状态管理器。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。

    Returns:
        StateManager: 标注状态管理器实例。
    Callers:
        - ``routes/annotations.py:apply_delete``
        - ``routes/annotations.py:deselect_all``
        - ``routes/annotations.py:get_state``
        - ``routes/annotations.py:restore``
        - ``routes/annotations.py:select_all``
        - ``routes/annotations.py:select_range``
        - ``routes/annotations.py:toggle``
    """
    return get_runtime(request).state_manager


def _rel_under(root: Path, rel: str) -> str:
    """校验相对路径并返回 posix 相对路径。

    Args:
        root (Path): 允许访问的根目录。
        rel (str): 待校验的相对路径。

    Returns:
        str: 相对 root 的 posix 相对路径。

    Callers:
        - ``routes/annotations.py:toggle``
        - ``routes/annotations.py:apply_delete``
        - ``routes/annotations.py:restore``
    """
    path = resolve_path(root, rel)
    return path.relative_to(root.resolve()).as_posix()


@router.post("/toggle")
def toggle(request: Request, body: ToggleBody) -> ToggleResponse:
    """切换单张图片的选中状态。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (ToggleBody): 含图片相对路径。

    Returns:
        ToggleResponse: 含路径与最新选中状态。

    Callers:
        - ``static/js/store.js:toggle``
    """
    path = _rel_under(_root(request), body.path)
    selected = _state(request).toggle(path)
    return ToggleResponse(path=path, selected=selected)


@router.post("/select-all")
def select_all(request: Request, body: SelectAllBody) -> SelectAllResponse:
    """全选当前视图的图片.

    工作目录视图与回收站视图互斥, 全选范围即视图可见集合: ``show_trash``
    为 False 时只选中 ``{sub}`` 下的存活图片, 为 True 时只选中
    ``.trash/{sub}`` 下的回收站图片. 删除与恢复消费同一选中集合,
    仅方向不同.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (SelectAllBody): 含目录列表、单个目录名与当前视图是否为回收站视图。

    Returns:
        SelectAllResponse: 含选中数量与目录列表。

    Callers:
        - ``static/js/store.js:selectAll``
    """
    root = _root(request)
    state_mgr = _state(request)
    names = body.dirs or parse_dir_names(None, body.dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    all_paths: list[str] = []
    for sub, work in pairs:
        if body.show_trash:
            trash_work = state_mgr.trash_dir / sub
            if trash_work.is_dir():
                all_paths.extend(img.path for img in scan_images(trash_work, path_prefix=sub))
        else:
            all_paths.extend(img.path for img in scan_images(work, path_prefix=sub))
    count = state_mgr.select_all(all_paths)
    return SelectAllResponse(selected=count, dirs=[s for s, _ in pairs])


@router.post("/deselect-all")
def deselect_all(request: Request) -> CountResponse:
    """取消全选。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。

    Returns:
        CountResponse: 含已选数量(恒为 0)。

    Callers:
        - ``static/js/store.js:deselectAll``
    """
    _state(request).deselect_all()
    return CountResponse(selected=0)


class SelectRangeBody(BaseModel):
    """范围选中请求."""

    paths: list[str] = Field(..., description="要选中的路径列表")


@router.post("/select-range")
def select_range(request: Request, body: SelectRangeBody) -> CountResponse:
    """将给定路径全部标为选中。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (SelectRangeBody): 含要选中的路径列表。

    Returns:
        CountResponse: 含已选数量。

    Callers:
        - ``static/js/store.js:selectRange``
    """
    count = _state(request).select_all(body.paths)
    return CountResponse(selected=count)


@router.post("/apply")
def apply_delete(request: Request) -> CountResponse:
    """将选中的图片移入回收站。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。

    Returns:
        CountResponse: 含已移动数量。

    Callers:
        - ``static/js/store.js:applyDelete``
    """
    root = _root(request)
    state_mgr = _state(request)
    state = state_mgr.get()
    selected = [_rel_under(root, p) for p in state.selected]
    if not selected:
        return CountResponse(moved=0)

    existing = [p for p in selected if (root / p).exists()]
    already_gone = [p for p in selected if not (root / p).exists()]
    moved = state_mgr.apply_delete(existing)

    if already_gone:
        state = state_mgr.get()
        for p in already_gone:
            state.trashed[p] = state.selected.pop(p, 0)
        state_mgr._write(state)
        moved += len(already_gone)

    idx = get_runtime(request).color_index
    if idx is not None:
        idx.clear()
    logger.info("数据集 dataset=%s 删除到回收站 %s 张 (%d 张源文件已不存在, 仅更新状态)",
                request.state.runtime.root.name, moved, len(already_gone))
    return CountResponse(moved=moved)


@router.post("/restore")
def restore(request: Request, body: RestoreBody) -> CountResponse:
    """从回收站恢复指定的选中图片.

    只恢复 ``body.paths`` 中已登记在回收站表里的路径; 恢复全部由前端
    先全选再传入完整路径列表完成, 本接口不设"空列表 = 全部"的隐含语义.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (RestoreBody): 含要恢复的路径列表。

    Returns:
        CountResponse: 含已恢复数量。

    Callers:
        - ``static/js/store.js:restore``
    """
    root = _root(request)
    state_mgr = _state(request)
    trashed = state_mgr.get().trashed
    to_restore = [_rel_under(root, p) for p in body.paths if p in trashed]
    if not to_restore:
        return CountResponse(restored=0)
    restored = state_mgr.restore(to_restore)
    idx = get_runtime(request).color_index
    if idx is not None:
        idx.clear()
    logger.info("数据集 dataset=%s 从回收站恢复 %s 张", request.state.runtime.root.name, restored)
    return CountResponse(restored=restored)


@router.get("/state")
def get_state(request: Request, dirs: str = "", dir: str = "main") -> AnnotatorStateResponse:
    """获取当前状态快照(含统计)。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        dirs (str): 逗号分隔的子目录名, 默认空。
        dir (str): 单个子目录名, 默认 ``main``。

    Returns:
        AnnotatorStateResponse: 含目录、总数、选中数、回收站数等统计。

    Callers:
        - ``static/js/api.js:getState``
    """
    root = _root(request)
    state_mgr = _state(request)
    names = parse_dir_names(dirs, dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    state = state_mgr.get()
    trashed = set(state.trashed.keys())
    selected_map = state.selected
    alive_n = 0
    trashed_n = 0
    selected_n = 0
    for sub, work in pairs:
        prefix = sub + "/"
        images = scan_images(work, path_prefix=sub)
        alive_n += sum(1 for img in images if img.path not in trashed)
        trashed_n += sum(1 for p in trashed if p.startswith(prefix))
        selected_n += sum(1 for p in selected_map if p.startswith(prefix))
    return AnnotatorStateResponse(
        dirs=[s for s, _ in pairs],
        dir=pairs[0][0],
        total=alive_n,
        selected=selected_n,
        trashed=trashed_n,
        active=alive_n,
    )
