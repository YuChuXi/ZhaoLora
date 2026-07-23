"""美学评分 API。"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
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
_REPORT_NAME = "aesthetic_report.html"


def _layout(request: Request) -> DatasetLayout:
    return request.app.state.layout


def _progress(request: Request) -> AestheticProgress:
    prog = getattr(request.app.state, "aesthetic_progress", None)
    if prog is None:
        prog = AestheticProgress()
        request.app.state.aesthetic_progress = prog
    return prog


def _report_path(root: Path) -> Path:
    return root / _REPORT_NAME


def rewrite_report_html(html: str) -> str:
    """把报告里的相对图片路径改成 /api/image?path=…，便于 iframe 内嵌。"""

    def fix_src(m: re.Match) -> str:
        url = m.group(1)
        if not url or url.startswith(("/api/", "http://", "https://", "data:")):
            return m.group(0)
        path = url.replace("\\", "/").lstrip("./")
        if not path:
            return m.group(0)
        return f'src="/api/image?path={quote(path, safe="/")}"'

    def fix_lightbox(m: re.Match) -> str:
        url = m.group(1)
        if not url or url.startswith(("/api/", "http://", "https://", "data:")):
            return m.group(0)
        path = url.replace("\\", "/").lstrip("./")
        if not path:
            return m.group(0)
        return f"openLightbox('/api/image?path={quote(path, safe='/')}'"

    html = re.sub(r'src="([^"]+)"', fix_src, html)
    html = re.sub(r"openLightbox\('([^']+)'", fix_lightbox, html)
    return html


class StartBody(BaseModel):
    dirs: list[str] = Field(default_factory=list)
    device: str = Field(default="cpu")
    write_report: bool = True


@router.get("/dirs")
def aesthetic_dirs(request: Request):
    root = _layout(request).root
    report = _report_path(root)
    return {
        "root": str(root),
        "dirs": list_image_subdirs(root),
        "cuda": cuda_available(),
        "default_device": default_device(),
        "report_ready": report.is_file(),
        "report_url": "/api/aesthetic/report" if report.is_file() else None,
    }


@router.get("/status")
def aesthetic_status(request: Request):
    snap = _progress(request).snapshot()
    root = _layout(request).root
    report = _report_path(root)
    snap["report_ready"] = report.is_file()
    snap["report_url"] = "/api/aesthetic/report" if report.is_file() else None
    if report.is_file():
        snap["report_mtime"] = report.stat().st_mtime
    return snap


@router.get("/report")
def aesthetic_report(request: Request):
    root = _layout(request).root
    path = _report_path(root)
    if not path.is_file():
        raise HTTPException(404, "报告尚未生成")
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(rewrite_report_html(html))


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
                    write_report=True,
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
        "status": prog.snapshot(),
    }


@router.post("/cancel")
def aesthetic_cancel(request: Request):
    prog = _progress(request)
    if prog.status != "running":
        raise HTTPException(400, "当前无运行中的任务")
    prog.cancel_requested = True
    return {"ok": True, "status": prog.snapshot()}
