"""
标注操作 API 路由 —— 选中、全选、取消全选、删除、恢复。

服务实例通过 Request.app.state 注入。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from services.scanner import scan_images
from services.work_dir import parse_dir_names, resolve_subdir, resolve_subdirs

router = APIRouter(prefix="/api", tags=["annotations"])


def _root(request: Request):
    return request.app.state.dataset_root


def _state(request: Request):
    return request.app.state.state_manager


@router.post("/toggle")
async def toggle(request: Request):
    """切换单张图片的选中状态。"""
    body = await request.json()
    path = body.get("path", "")
    if not path:
        return {"error": "Missing 'path'"}

    selected = _state(request).toggle(path)
    return {"path": path, "selected": selected}


@router.post("/select-all")
async def select_all(request: Request):
    """全选当前选中子目录中未被删除的图片。"""
    body = await request.json()
    root = _root(request)
    state_mgr = _state(request)
    names = body.get("dirs") or parse_dir_names(None, body.get("dir"))
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    trashed = set(state_mgr.get().get("trashed", {}).keys())
    all_paths = []
    for sub, work in pairs:
        all_paths.extend(
            img["path"] for img in scan_images(work, path_prefix=sub)
            if img["path"] not in trashed
        )
    count = state_mgr.select_all(all_paths)
    return {"selected": count, "dirs": [s for s, _ in pairs]}


@router.post("/deselect-all")
def deselect_all(request: Request):
    """取消全选。"""
    _state(request).deselect_all()
    return {"selected": 0}


@router.post("/apply")
def apply_delete(request: Request):
    """将选中的图片移入回收站。"""
    root = _root(request)
    state_mgr = _state(request)
    state = state_mgr.get()
    selected = list(state.get("selected", {}).keys())

    if not selected:
        return {"moved": 0}

    existing = [p for p in selected if (root / p).exists()]
    already_gone = [p for p in selected if not (root / p).exists()]

    moved = state_mgr.apply_delete(existing)

    if already_gone:
        state = state_mgr.get()
        for p in already_gone:
            state["trashed"][p] = state["selected"].pop(p, 0)
        state_mgr._write(state)
        moved += len(already_gone)

    idx = getattr(request.app.state, "color_index", None)
    if idx is not None:
        idx.clear()

    return {"moved": moved}


@router.post("/restore")
async def restore(request: Request):
    """从回收站恢复图片。"""
    state_mgr = _state(request)
    body = await request.json()
    paths: list[str] = body.get("paths", [])

    trashed = state_mgr.get().get("trashed", {})
    if paths:
        to_restore = [p for p in paths if p in trashed]
    else:
        to_restore = list(trashed.keys())

    if not to_restore:
        return {"restored": 0}

    restored = state_mgr.restore(to_restore)
    idx = getattr(request.app.state, "color_index", None)
    if idx is not None:
        idx.clear()
    return {"restored": restored}


@router.get("/state")
def get_state(request: Request, dirs: str = "", dir: str = "main"):
    """获取当前状态快照（含统计）。"""
    root = _root(request)
    state_mgr = _state(request)
    names = parse_dir_names(dirs, dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    state = state_mgr.get()
    trashed = set(state.get("trashed", {}).keys())
    selected_map = state.get("selected", {})
    alive_n = 0
    trashed_n = 0
    selected_n = 0
    for sub, work in pairs:
        prefix = sub + "/"
        images = scan_images(work, path_prefix=sub)
        alive_n += sum(1 for img in images if img["path"] not in trashed)
        trashed_n += sum(1 for p in trashed if p.startswith(prefix))
        selected_n += sum(1 for p in selected_map if p.startswith(prefix))
    return {
        "dirs": [s for s, _ in pairs],
        "dir": pairs[0][0],
        "total": alive_n,
        "selected": selected_n,
        "trashed": trashed_n,
        "active": alive_n,
    }
