#!/usr/bin/env python3
"""
网页图片标注工具. 

配置经 ``web_annotator/.env`` 与 ``ANNOTATOR_*`` 环境变量提供, 应用初始化在 FastAPI
``lifespan`` 中完成; 任务根由 ``ANNOTATOR_DATA_DIR`` 指定(如 datasets/), 任务须在界面
手动加入清单 (datasets/.annotator_tasks.json), 不会因磁盘子目录自动出现. 

用法 (从仓库根目录启动):
  .venv/bin/uvicorn server:app --app-dir web_annotator --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.gzip import GZipMiddleware



from routes.alpha_paint import router as paint_router
from routes.annotations import router as annotations_router
from routes.scoring import router as scoring_router
from routes.birefnet import router as birefnet_router
from routes.online_sources import router as sources_router
from routes.crop import router as crop_router
from routes.dedupe import router as dedupe_router
from routes.docs import router as docs_router
from routes.images import router as images_router
from routes.meta import datasets_router, jobs_router
from routes.ready import router as ready_router
from routes.tag_stats import router as tag_stats_router
from routes.tagging import router as tagging_router
from routes.tasks import router as tasks_router
from routes.tasks import archive_router as archived_router
from routes.video import router as video_router
from services.dataset.runtime import DatasetRegistry
from services.core.jobs import JobManager
from services.core.logging import setup_logging
from services.state.tasks import TaskStore

# 启动前加载 web_annotator/.env (与当前工作目录无关), 供数据源凭据与服务器配置读取.
load_dotenv(Path(__file__).parent / ".env")

# 访问日志专用 logger, 经根日志器统一格式化输出.
_ACCESS_LOG = logging.getLogger("web_annotator.access")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 启动时装配日志与运行时依赖, 关闭时释放.

    服务器配置直接经 ``ANNOTATOR_*`` 环境变量读取, 与各数据源模块自读环境变量
    的方式一致; 任务存储/任务运行时/任务管理器挂载到 ``app.state``, 供各路由经
    ``request.app.state`` 访问. 经 ``uvicorn server:app`` 命令启动时本生命周期完整执行.

    Args:
        app: FastAPI 应用实例, 其 ``state`` 用于承载全局依赖.

    Yields:
        None: 应用运行期间挂起; 关闭时无额外清理.

    Callers:
        - ``server.py:`` (FastAPI ``lifespan=`` 参数传入, 由 ASGI 服务器调度)
    """
    raw_data_dir = os.environ.get("ANNOTATOR_DATA_DIR", "").strip()
    if not raw_data_dir:
        raise RuntimeError("缺少环境变量 ANNOTATOR_DATA_DIR")
    data_dir = Path(raw_data_dir).resolve()
    if not data_dir.is_dir():
        raise RuntimeError(f"任务根目录不存在: {data_dir}")
    thumbnail_size = int(os.environ.get("ANNOTATOR_THUMBNAIL_SIZE", "1280"))
    log_level = os.environ.get("ANNOTATOR_LOG_LEVEL", "info").strip()
    log_dir = (
        Path(os.environ["ANNOTATOR_LOG_DIR"]).resolve()
        if os.environ.get("ANNOTATOR_LOG_DIR", "").strip()
        else None
    )
    block_chinese_ua = os.environ.get("ANNOTATOR_BLOCK_CHINESE_UA", "").lower() in (
        "1",
        "true",
        "yes",
    )

    setup_logging(log_level, log_dir or (data_dir / "logs"))
    log = logging.getLogger("web_annotator")

    store = TaskStore(data_dir)
    jobs = JobManager()
    registry = DatasetRegistry(
        task_root=data_dir,
        thumbnail_size=thumbnail_size,
        job_manager=jobs,
    )

    app.state.task_store = store
    app.state.datasets = registry
    app.state.jobs = jobs
    app.state.paint_default_alpha = 255
    app.state.block_chinese_ua = block_chinese_ua

    log.info("任务根   : %s", data_dir)
    log.info("清单任务 : %s", len(store.list_entries()))
    if block_chinese_ua:
        log.info("UA 拦截  : 已启用(屏蔽 QQ/微信/国产浏览器)")

    yield


app = FastAPI(title="Image Annotator", version="3.1.0", lifespan=lifespan)

_PAGE_TEMPLATES = {
    "filter": "filter.html",
    "scoring": "scoring.html",
    "dedupe": "dedupe.html",
    "crop": "crop.html",
    "paint": "paint.html",
    "tags": "tags.html",
    "tag-check": "tag_check.html",
    "ready": "ready.html",
    "video_extract": "video_extract.html",
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
    """读取 ``templates`` 目录下 HTML 模板并返回 ``HTMLResponse``.

    Args:
        name: ``templates`` 目录下的 HTML 文件名.

    Returns:
        HTMLResponse: 文件内容响应; 文件缺失时返回 404 提示页.

    Callers:
        - ``server.py:_block_chinese_ua_middleware``
        - ``server.py:index_page``
        - ``server.py:dataset_page``
    """
    template = Path(__file__).parent / "templates" / name
    if not template.exists():
        return HTMLResponse(f"<h1>{name} not found</h1>", status_code=404)
    return HTMLResponse(template.read_text(encoding="utf-8"))


@app.middleware("http")
async def bind_dataset_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """从路径解析任务名；须在清单中, 再按需加载运行时.

    写入 ``request.state.runtime``, 由下游 ``routes/deps.py:get_runtime`` 读取.

    Args:
        request: 当前 HTTP 请求; 路径匹配 ``/d/{name}/...`` 时触发数据集绑定.
        call_next: ASGI 链中下一个中间件/路由处理器.

    Returns:
        Response: 下游响应; 任务名未知时返回 404 JSON.

    Callers:
        - ``server.py:`` (FastAPI ``@app.middleware`` 装饰器注册, 由 Starlette ASGI 框架调度)
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


@app.middleware("http")
async def _access_log_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """为每个 HTTP 请求记录统一访问日志.

    记录方法、路径、响应状态码与处理耗时, 输出到 ``web_annotator.access`` logger,
    经根日志器统一格式化, 供审计与性能观察。

    Args:
        request: 当前 HTTP 请求.
        call_next: ASGI 链中下一个中间件/路由处理器.

    Returns:
        Response: 下游响应, 原样透传.

    Callers:
        - ``server.py:`` (FastAPI ``@app.middleware`` 装饰器注册, 由 Starlette ASGI 框架调度)
    """
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    _ACCESS_LOG.info(
        "%s %s %d %.1fms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# 对超过阈值(默认 500)的响应启用 gzip, 避免小响应压缩反增开销。
# 使用 Starlette 内置 GZipMiddleware, 其基于 ASGI 直接改写响应头,
# 正确处理 Content-Length 与 Vary, 无需手写压缩逻辑。
app.add_middleware(GZipMiddleware, minimum_size=1000)


static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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
dataset_api.include_router(sources_router)
dataset_api.include_router(birefnet_router)
dataset_api.include_router(video_router)

app.include_router(dataset_api)
app.include_router(tasks_router)
app.include_router(archived_router)
app.include_router(datasets_router)
app.include_router(jobs_router)
app.include_router(docs_router)


@app.get("/")
def index_page() -> HTMLResponse:
    """任务与文档首页.

    Returns:
        HTMLResponse: ``index.html`` 渲染结果.

    Callers:
        - ``static/js/nav.js:render`` (业务页"首页"标签链接)
    """
    return _read_template("index.html")


@app.get("/d/{dataset}")
def dataset_root_redirect(dataset: str) -> RedirectResponse:
    """任务根路径重定向到筛选页.

    Args:
        dataset: 任务名; 须已在任务清单中(由 ``bind_dataset_middleware`` 校验).

    Returns:
        RedirectResponse: 302 重定向到 ``/d/{dataset}/filter``.
    """
    return RedirectResponse(url=f"/d/{dataset}/filter", status_code=302)


@app.get("/d/{dataset}/thumb/{filename}")
async def dataset_thumb(request: Request, dataset: str, filename: str) -> FileResponse:
    """返回数据集的 AVIF 缩略图文件, 缺失时即时生成.

    Callers:
        - ``static/js/index.js:archiveTask``
        - ``static/js/api.js:thumbnailUrl``
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
            await thumb.ensure_async(rel)
    if not path.is_file():
        raise HTTPException(404, "缩略图不存在")
    return FileResponse(path, media_type="image/avif", headers={"Cache-Control": "no-store"})


@app.get("/d/{dataset}/{page}", response_model=None)
def dataset_page(dataset: str, page: str) -> HTMLResponse | RedirectResponse:
    """业务页面. 任务名须已在清单中(中间件校验).

    Args:
        dataset: 任务名; 须已在任务清单中(由 ``bind_dataset_middleware`` 校验).
        page: 业务页标识; 命中 ``_PAGE_TEMPLATES`` 返回对应模版, ``tagging`` 重定向到 ``tags``.

    Returns:
        HTMLResponse | RedirectResponse: 页面 HTML; ``tagging`` 返回 302 重定向, 未知页抛 404.

    Callers:
        - ``static/js/nav.js:render`` (功能标签链接: 筛选/评分/去重/裁剪/蒙版/标签/标签检查/就绪)
    """
    if page == "tagging":
        return RedirectResponse(url=f"/d/{dataset}/tags", status_code=302)
    tpl = _PAGE_TEMPLATES.get(page)
    if tpl is None:
        raise HTTPException(404, f"未知页面: {page}")
    return _read_template(tpl)
