"""
标注操作 API 路由 —— 选中、全选、取消全选、删除、恢复。

服务实例通过 Request.app.state 注入。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from services.scanner import scan_images

router = APIRouter(prefix="/api", tags=["annotations"])


def _services(request: Request):
    return request.app.state.data_dir, request.app.state.state_manager


@router.post("/toggle")
async def toggle(request: Request):
    """切换单张图片的选中状态。"""
    body = await request.json()
    path = body.get("path", "")
    if not path:
        return {"error": "Missing 'path'"}

    _, state_mgr = _services(request)
    selected = state_mgr.toggle(path)
    return {"path": path, "selected": selected}


@router.post("/select-all")
def select_all(request: Request):
    """全选所有未被删除的图片。"""
    data_dir, state_mgr = _services(request)
    trashed = set(state_mgr.get().get("trashed", {}).keys())
    all_paths = [img["path"] for img in scan_images(data_dir) if img["path"] not in trashed]
    count = state_mgr.select_all(all_paths)
    return {"selected": count}


@router.post("/deselect-all")
def deselect_all(request: Request):
    """取消全选。"""
    _, state_mgr = _services(request)
    state_mgr.deselect_all()
    return {"selected": 0}


@router.post("/apply")
def apply_delete(request: Request):
    """将选中的图片移入回收站。"""
    data_dir, state_mgr = _services(request)
    state = state_mgr.get()
    selected = list(state.get("selected", {}).keys())

    if not selected:
        return {"moved": 0}

    existing = [p for p in selected if (data_dir / p).exists()]
    already_gone = [p for p in selected if not (data_dir / p).exists()]

    moved = state_mgr.apply_delete(existing)

    if already_gone:
        state = state_mgr.get()
        for p in already_gone:
            state["trashed"][p] = state["selected"].pop(p, 0)
        state_mgr._write(state)
        moved += len(already_gone)

    return {"moved": moved}


@router.post("/restore")
async def restore(request: Request):
    """从回收站恢复图片。"""
    _, state_mgr = _services(request)
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
    return {"restored": restored}


@router.get("/state")
def get_state(request: Request):
    """获取当前状态快照（含统计）。"""
    data_dir, state_mgr = _services(request)
    state = state_mgr.get()
    images = scan_images(data_dir)
    total = len(images)
    selected = len(state.get("selected", {}))
    trashed = len(state.get("trashed", {}))
    return {
        "total": total,
        "selected": selected,
        "trashed": trashed,
        "active": total - trashed,
    }
