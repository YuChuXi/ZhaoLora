"""质量评分 API（数据集作用域）。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs
from services.jobs import EmitFn
from services.quality import (
    QualityProgress,
    collect_image_dirs,
    cuda_available,
    default_device,
    report_path,
    run_quality,
)
from services.thumbnailer import Thumbnailer

router = APIRouter(prefix="/quality", tags=["quality"])


class QualityDirsResponse(BaseModel):
    """可评分目录列表。"""

    root: str
    dirs: list[str]
    cuda: bool
    default_device: str
    report_ready: bool
    report_url: str | None


class QualityStartBody(BaseModel):
    """质量评分启动请求。"""

    dirs: list[str] = Field(default_factory=list)
    device: str = Field(default="cpu")
    write_report: bool = True


class QualityStartResponse(BaseModel):
    """质量评分任务已提交。"""

    ok: bool = True
    job_id: str


class QualityActiveResponse(BaseModel):
    """重连已有任务。"""

    ok: bool = True
    job_id: str | None = None
    status: str = "idle"


def _normalize_report_img_path(url: str) -> str | None:
    """将报告 HTML 中的相对图片路径规范为数据集根相对路径。"""
    path = (url or "").replace("\\", "/").split("?", 1)[0].strip()
    if not path or path.startswith(("http://", "https://", "data:", "/api/", "/thumb/", "/d/")):
        return None
    while path.startswith("./"):
        path = path[2:]
    parts: list[str] = []
    for p in path.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if parts:
                parts.pop()
            continue
        parts.append(p)
    return "/".join(parts) if parts else None


def rewrite_report_html(html: str, thumbnailer: Thumbnailer, dataset: str) -> str:
    """将报告卡片图片替换为缩略图 URL，灯箱保持原图。

    Callers:
        - `routes/quality.py:quality_report`
    """

    def fix_src(m: re.Match) -> str:
        url = m.group(1)
        rel = _normalize_report_img_path(url)
        if rel is None:
            return m.group(0)
        thumbnailer.ensure(rel)
        h = thumbnailer.hash_for(rel)
        return f'src="/d/{quote(dataset, safe="")}/thumb/{h}.avif"'

    def fix_lightbox(m: re.Match) -> str:
        url = m.group(1)
        rel = _normalize_report_img_path(url)
        if rel is None:
            return m.group(0)
        return (
            f"openLightbox('/api/d/{quote(dataset, safe='')}/image?"
            f"path={quote(rel, safe='/')}'"
        )

    html = re.sub(r'src="([^"]+)"', fix_src, html)
    html = re.sub(r"openLightbox\('([^']+)'", fix_lightbox, html)
    return html


@router.get("/dirs")
def quality_dirs(request: Request) -> QualityDirsResponse:
    """列出可评分目录与报告状态。

    Callers:
        - `quality.js`
    """
    rt = get_runtime(request)
    root = rt.root
    rp = report_path(root)
    return QualityDirsResponse(
        root=str(root),
        dirs=[d.name for d in collect_image_dirs(root)],
        cuda=cuda_available(),
        default_device=default_device(),
        report_ready=rp.is_file(),
        report_url=f"/api/d/{rt.name}/quality/report" if rp.is_file() else None,
    )


@router.get("/report")
def quality_report(request: Request) -> HTMLResponse:
    """返回改写后的质量报告 HTML。

    Callers:
        - `quality.js`
    """
    rt = get_runtime(request)
    rp = report_path(rt.root)
    if not rp.is_file():
        raise HTTPException(404, "报告尚未生成")
    html = rp.read_text(encoding="utf-8")
    return HTMLResponse(rewrite_report_html(html, rt.thumbnailer, rt.name))


@router.post("/start")
async def quality_start(request: Request, body: QualityStartBody) -> QualityStartResponse:
    """提交质量评分长任务。

    Callers:
        - `quality.js`
    """
    rt = get_runtime(request)
    root = rt.root

    all_dirs = collect_image_dirs(root)
    dir_set = set(body.dirs) if body.dirs else set()
    selected = [d for d in all_dirs if d.name in dir_set]
    if not selected:
        raise HTTPException(400, "请选择至少一个目录")

    prog = QualityProgress()

    def run(emit: EmitFn) -> None:
        def on_update() -> None:
            emit(prog.snapshot())

        run_quality(
            dirs=selected,
            root=root,
            device=body.device,
            write_report=body.write_report,
            progress=prog,
            on_update=on_update,
        )
        snap = prog.snapshot()
        snap.status = "done"
        emit(snap)

    job_id = jobs(request).submit(
        kind="quality",
        group="compute",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    return QualityStartResponse(job_id=job_id)


@router.get("/active")
def quality_active(request: Request) -> QualityActiveResponse:
    """检查是否有正在运行的质量评分任务（用于页面重连）。

    Callers:
        - `quality.js`
    """
    rt = get_runtime(request)
    jm = jobs(request)
    for job in jm.list_running():
        if job.kind == "quality" and job.dataset == rt.name:
            return QualityActiveResponse(job_id=job.id, status=job.status)
    return QualityActiveResponse()
