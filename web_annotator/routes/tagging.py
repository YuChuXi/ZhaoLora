"""WD14 批量打标 API。"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.dataset_layout import DatasetLayout
from services.wd14_batch import (
    DEFAULT_THRESHOLD,
    TaggingProgress,
    list_image_subdirs,
    model_ready,
    resolve_image_dirs,
    run_batch,
)

router = APIRouter(prefix="/api/tagging", tags=["tagging"])

_JOB_LOCK = threading.Lock()


def _layout(request: Request) -> DatasetLayout:
    return request.app.state.layout


def _progress(request: Request) -> TaggingProgress:
    prog = getattr(request.app.state, "tagging_progress", None)
    if prog is None:
        prog = TaggingProgress()
        request.app.state.tagging_progress = prog
    return prog


class StartBody(BaseModel):
    dirs: list[str] = Field(default_factory=list, description="子目录名")
    threshold: float = Field(default=DEFAULT_THRESHOLD, ge=0.0, le=1.0)
    disable_orig_tags: bool = True
    force: bool = True


@router.get("/dirs")
def tagging_dirs(request: Request):
    root = _layout(request).root
    ok, model_path = model_ready()
    return {
        "root": str(root),
        "dirs": list_image_subdirs(root),
        "model_ready": ok,
        "model_path": model_path,
        "default_threshold": DEFAULT_THRESHOLD,
    }


@router.get("/status")
def tagging_status(request: Request):
    return _progress(request).snapshot()


@router.post("/start")
def tagging_start(request: Request, body: StartBody):
    root = _layout(request).root
    ok, msg = model_ready()
    if not ok:
        raise HTTPException(400, msg)

    selected = resolve_image_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    with _JOB_LOCK:
        prog = _progress(request)
        if prog.status == "running":
            raise HTTPException(409, "已有任务在运行")
        prog = TaggingProgress()
        request.app.state.tagging_progress = prog

        def worker():
            try:
                run_batch(
                    selected,
                    threshold=body.threshold,
                    disable_orig_tags=body.disable_orig_tags,
                    force=body.force,
                    progress=prog,
                )
            except Exception as e:
                prog.status = "error"
                prog.message = str(e)
                prog.log(f"任务异常: {e}")

        threading.Thread(target=worker, daemon=True).start()

    return {
        "ok": True,
        "dirs": [p.name if p != root else "." for p in selected],
        "threshold": body.threshold,
        "disable_orig_tags": body.disable_orig_tags,
        "force": body.force,
        "status": prog.snapshot(),
    }


@router.post("/cancel")
def tagging_cancel(request: Request):
    prog = _progress(request)
    if prog.status != "running":
        raise HTTPException(400, "当前无运行中的任务")
    prog.cancel_requested = True
    return {"ok": True, "status": prog.snapshot()}
