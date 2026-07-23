"""美学评分 API。"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.aesthetic import (
    AestheticProgress,
    cuda_available,
    default_device,
    list_image_subdirs,
    resolve_image_dirs,
    run_aesthetic,
)
from services.dataset_layout import DatasetLayout

router = APIRouter(prefix="/api/aesthetic", tags=["aesthetic"])

_JOB_LOCK = threading.Lock()


def _layout(request: Request) -> DatasetLayout:
    return request.app.state.layout


def _progress(request: Request) -> AestheticProgress:
    prog = getattr(request.app.state, "aesthetic_progress", None)
    if prog is None:
        prog = AestheticProgress()
        request.app.state.aesthetic_progress = prog
    return prog


class StartBody(BaseModel):
    dirs: list[str] = Field(default_factory=list)
    device: str = Field(default="cpu")
    rename: bool = True
    write_report: bool = True


@router.get("/dirs")
def aesthetic_dirs(request: Request):
    root = _layout(request).root
    return {
        "root": str(root),
        "dirs": list_image_subdirs(root),
        "cuda": cuda_available(),
        "default_device": default_device(),
    }


@router.get("/status")
def aesthetic_status(request: Request):
    return _progress(request).snapshot()


@router.post("/start")
def aesthetic_start(request: Request, body: StartBody):
    root = _layout(request).root
    selected = resolve_image_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    device = (body.device or "cpu").lower()
    if device not in ("cpu", "cuda"):
        raise HTTPException(400, "设备须为 cpu 或 cuda")
    if device == "cuda" and not cuda_available():
        raise HTTPException(400, "当前环境无 CUDA")

    with _JOB_LOCK:
        prog = _progress(request)
        if prog.status == "running":
            raise HTTPException(409, "已有任务在运行")
        prog = AestheticProgress()
        request.app.state.aesthetic_progress = prog

        def worker():
            try:
                run_aesthetic(
                    selected,
                    root=root,
                    device=device,
                    rename=body.rename,
                    write_report=body.write_report,
                    progress=prog,
                )
            except Exception as e:
                prog.status = "error"
                prog.phase = "done"
                prog.message = str(e)
                prog.log(f"任务异常: {e}")

        threading.Thread(target=worker, daemon=True).start()

    return {
        "ok": True,
        "dirs": [p.name if p != root else "." for p in selected],
        "device": device,
        "rename": body.rename,
        "status": prog.snapshot(),
    }


@router.post("/cancel")
def aesthetic_cancel(request: Request):
    prog = _progress(request)
    if prog.status != "running":
        raise HTTPException(400, "当前无运行中的任务")
    prog.cancel_requested = True
    return {"ok": True, "status": prog.snapshot()}
