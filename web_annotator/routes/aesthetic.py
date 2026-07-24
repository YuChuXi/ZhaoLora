"""美学评分 API（数据集作用域）。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs
from services.aesthetic import (
    AestheticProgress,
    cuda_available,
    default_device,
    list_image_subdirs,
    resolve_image_dirs,
    run_aesthetic,
)
from services.models import AestheticDirsResponse, AestheticStartResponse
from services.thumbnailer import Thumbnailer
from services.jobs import EmitFn

router = APIRouter(prefix="/aesthetic", tags=["aesthetic"])

_REPORT_NAME = "aesthetic_report.html"


def _report_path(root: Path) -> Path:
    """美学报告 HTML 路径。引用：本模块各接口。"""
    return root / _REPORT_NAME


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
    """
    报告卡片用缩略图；灯箱仍用原图。

    引用：aesthetic_report 路由。
    """

    def fix_src(m: re.Match) -> str:
        """把卡片 ``src`` 换成缩略图 URL。引用：``rewrite_report_html``。"""
        url = m.group(1)
        rel = _normalize_report_img_path(url)
        if rel is None:
            return m.group(0)
        thumbnailer.ensure(rel)
        h = thumbnailer.hash_for(rel)
        return f'src="/d/{quote(dataset, safe="")}/thumb/{h}.avif"'

    def fix_lightbox(m: re.Match) -> str:
        """把灯箱链接换成原图 API。引用：``rewrite_report_html``。"""
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


class StartBody(BaseModel):
    """美学评分启动请求。引用：``POST /aesthetic/start``。"""

    dirs: list[str] = Field(default_factory=list)
    device: str = Field(default="cpu")
    write_report: bool = True


@router.get("/dirs")
def aesthetic_dirs(request: Request) -> AestheticDirsResponse:
    """列出可评分目录与报告状态。引用：``aesthetic.js``。"""
    rt = get_runtime(request)
    root = rt.root
    report = _report_path(root)
    return AestheticDirsResponse(
        root=str(root),
        dirs=list_image_subdirs(root),
        cuda=cuda_available(),
        default_device=default_device(),
        report_ready=report.is_file(),
        report_url=f"/api/d/{rt.name}/aesthetic/report" if report.is_file() else None,
    )


@router.get("/report")
def aesthetic_report(request: Request) -> HTMLResponse:
    """返回改写后的美学报告 HTML。引用：``aesthetic.js``。"""
    rt = get_runtime(request)
    path = _report_path(rt.root)
    if not path.is_file():
        raise HTTPException(404, "报告尚未生成")
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(rewrite_report_html(html, rt.thumbnailer, rt.name))


@router.post("/start")
async def aesthetic_start(request: Request, body: StartBody) -> AestheticStartResponse:
    """
    提交美学评分长任务。

    返回 job_id；进度见 GET /api/jobs/{id}/events。
    引用：aesthetic.js。
    """
    rt = get_runtime(request)
    root = rt.root
    selected = resolve_image_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    device = (body.device or "cpu").lower()
    if device not in ("cpu", "cuda"):
        raise HTTPException(400, "设备须为 cpu 或 cuda")
    if device == "cuda" and not cuda_available():
        raise HTTPException(400, "当前环境无 CUDA")

    prog = AestheticProgress()
    report_url = f"/api/d/{rt.name}/aesthetic/report"

    def run(emit: EmitFn) -> None:
        """工作线程入口。引用：``JobManager.submit``。"""
        def on_update() -> None:
            """推送美学进度快照。引用：``run_aesthetic``。"""
            report = _report_path(root)
            ready = report.is_file()
            snap = prog.snapshot().model_copy(
                update={
                    "report_ready": ready,
                    "report_url": report_url if ready else None,
                    "report_mtime": report.stat().st_mtime if ready else None,
                }
            )
            emit(snap)

        run_aesthetic(
            selected,
            root=root,
            device=device,
            write_report=True,
            progress=prog,
            on_update=on_update,
        )
        on_update()

    job_id = jobs(request).submit(
        kind="aesthetic",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    return AestheticStartResponse(
        ok=True,
        job_id=job_id,
        dirs=[p.name if p != root else "." for p in selected],
        device=device,
    )
