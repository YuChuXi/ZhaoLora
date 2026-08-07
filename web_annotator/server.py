#!/usr/bin/env python3
"""
网页图片标注工具. 

--data-dir 指向任务根(如 datasets/). 任务须在界面手动加入清单
(datasets/.annotator_tasks.json), 不会因磁盘子目录自动出现. 

用法:
  python web_annotator/server.py --data-dir datasets
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import uvicorn



from routes.alpha_paint import router as paint_router
from routes.annotations import router as annotations_router
from routes.scoring import router as scoring_router
from routes.birefnet import router as birefnet_router
from routes.booru import router as booru_router
from routes.crop import router as crop_router
from routes.dedupe import router as dedupe_router
from routes.docs import router as docs_router
from routes.images import router as images_router
from routes.meta import datasets_router, jobs_router
from routes.ready import router as ready_router
from routes.tag_stats import router as tag_stats_router
from routes.tagging import router as tagging_router
from routes.tasks import router as tasks_router
from services.dataset.runtime import DatasetRegistry
from services.core.jobs import JobManager
from services.state.tasks import TaskStore


class _DevStaticFiles(StaticFiles):
    """开发期静态资源不缓存, 避免改 JS 后浏览器仍用旧文件. """

    def file_response(self, *args, **kwargs) -> Response:
        """关闭静态资源缓存. 引用: 开发期改 JS 后立即生效. """
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store"
        return resp


app = FastAPI(title="Image Annotator", version="3.1.0")

_PAGE_TEMPLATES = {
    "filter": "index.html",
    "scoring": "scoring.html",
    "dedupe": "dedupe.html",
    "crop": "crop.html",
    "paint": "paint.html",
    "tags": "tags.html",
    "tag-check": "tag_check.html",
    "ready": "ready.html",
}

_DS_PATH_RE = re.compile(r"^/(?:api/)?d/([^/]+)(?:/|$)")

_CHINESE_UA_PATTERNS = re.compile(
    r"QQBrowser|MQQBrowser|MicroMessenger|UCBrowser|UCWEB|"
    r"baidubrowser|Baidu(?!.*AppleWebKit)|"
    r"360SE|360EE|360Browser|"
    r"Sogou|MetaSr|Maxthon|LieBao|2345Explorer|Quark",
    re.IGNORECASE,
)

async def _block_chinese_ua_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if not request.app.state.block_chinese_ua:
        return await call_next(request)
    ua = request.headers.get("user-agent", "")
    if _CHINESE_UA_PATTERNS.search(ua):
        resp = _read_template("blocked.html")
        resp.status_code = 403
        return resp
    return await call_next(request)


def _read_template(name: str) -> HTMLResponse:
    """读取 templates 下 HTML. 引用: home_page / dataset_page. """
    template = Path(__file__).parent / "templates" / name
    if not template.exists():
        return HTMLResponse(f"<h1>{name} not found</h1>", status_code=404)
    return HTMLResponse(template.read_text(encoding="utf-8"))


@app.middleware("http")
async def bind_dataset_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """
    从路径解析任务名；须在清单中, 再按需加载运行时. 

    引用: routes/deps.get_runtime. 
    """
    m = _DS_PATH_RE.match(request.url.path)
    if m:
        name = m.group(1)
        store: TaskStore = request.app.state.task_store
        if not store.contains(name):
            return JSONResponse({"detail": f"未知任务: {name}"}, status_code=404)
        reg: DatasetRegistry = request.app.state.datasets
        request.state.runtime = await reg.ensure(name)
    return await call_next(request)


@app.middleware("http")
async def _ua_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    return await _block_chinese_ua_middleware(request, call_next)


static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", _DevStaticFiles(directory=str(static_dir)), name="static")

dataset_api = APIRouter(prefix="/api/d/{dataset}")
dataset_api.include_router(images_router)
dataset_api.include_router(annotations_router)
dataset_api.include_router(paint_router)
dataset_api.include_router(crop_router)
dataset_api.include_router(scoring_router)
dataset_api.include_router(tagging_router)
dataset_api.include_router(tag_stats_router)
dataset_api.include_router(dedupe_router)
dataset_api.include_router(ready_router)
dataset_api.include_router(booru_router)
dataset_api.include_router(birefnet_router)

app.include_router(dataset_api)
app.include_router(tasks_router)
app.include_router(datasets_router)
app.include_router(jobs_router)
app.include_router(docs_router)


@app.get("/")
def home_page() -> HTMLResponse:
    """任务与文档首页. 引用: 浏览器打开根路径. """
    return _read_template("home.html")


@app.get("/d/{dataset}")
def dataset_root_redirect(dataset: str) -> RedirectResponse:
    """任务根路径重定向到筛选页. 引用: 浏览器打开 ``/d/{name}``. """
    return RedirectResponse(url=f"/d/{dataset}/filter", status_code=302)


@app.get("/d/{dataset}/thumb/{filename}")
async def dataset_thumb(request: Request, dataset: str, filename: str) -> FileResponse:
    """返回数据集的 AVIF 缩略图文件, 缺失时即时生成.

    Callers:
        - `static/js/home.js:211`
        - `static/js/api.js:120`
    """
    store: TaskStore = request.app.state.task_store
    if not store.contains(dataset):
        raise HTTPException(404, f"未知任务: {dataset}")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    rt = await request.app.state.datasets.ensure(dataset)
    thumb = rt.thumbnailer
    path = thumb.thumb_dir / filename
    if not path.is_file():
        rel = thumb.find_rel_path(filename)
        if rel is not None:
            thumb.generate(rel)
    if not path.is_file():
        raise HTTPException(404, "缩略图不存在")
    return FileResponse(path, media_type="image/avif", headers={"Cache-Control": "no-store"})


@app.get("/d/{dataset}/{page}", response_model=None)
def dataset_page(dataset: str, page: str) -> HTMLResponse | RedirectResponse:
    """
    业务页面. 任务名须已在清单中(中间件校验). 

    引用: nav.js 功能标签. 
    """
    if page == "tagging":
        return RedirectResponse(url=f"/d/{dataset}/tags", status_code=302)
    tpl = _PAGE_TEMPLATES.get(page)
    if tpl is None:
        raise HTTPException(404, f"未知页面: {page}")
    return _read_template(tpl)


def main() -> int:
    """启动 uvicorn. 引用: ``python web_annotator/server.py``. """
    load_dotenv()

    parser = argparse.ArgumentParser(description="网页图片标注工具")
    parser.add_argument(
        "--data-dir",
        type=str,
        required=True,
        help="任务根目录, 例如 datasets/(任务须手动加入清单)",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--thumbnail-size", type=int, default=2560, help="缩略图长边像素")
    parser.add_argument(
        "--block-chinese-ua",
        action="store_true",
        default=False,
        help="屏蔽 QQ/微信/国产浏览器(UC/360/搜狗/百度/夸克等)",
    )
    args = parser.parse_args()

    task_root = Path(args.data_dir).resolve()
    if not task_root.is_dir():
        print(f"错误: 目录不存在: {task_root}")
        return 1

    store = TaskStore(task_root)
    jobs = JobManager()
    registry = DatasetRegistry(
        task_root=task_root,
        thumbnail_size=args.thumbnail_size,
        job_manager=jobs,
    )

    app.state.task_store = store
    app.state.datasets = registry
    app.state.jobs = jobs
    app.state.upstream_proxy = os.environ.get("UPSTREAM_PROXY", "").strip() or None
    app.state.paint_default_alpha = 255
    app.state.block_chinese_ua = args.block_chinese_ua

    n = len(store.list_entries())
    print(f"任务根   : {task_root}")
    print(f"清单任务 : {n}")
    print(f"服务地址 : http://{args.host}:{args.port}")
    if args.block_chinese_ua:
        print("UA 拦截  : 已启用(屏蔽 QQ/微信/国产浏览器)")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())