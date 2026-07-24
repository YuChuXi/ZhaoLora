#!/usr/bin/env python3
"""
网页图片标注工具。

目录约定（--data-dir = datasets/xxx）:
  datasets/xxx/
    main/     # 源图
    mask/     # alpha mask
    head/     # 部位裁剪
    foot/
    crops/

用法:
  python web_annotator/server.py --data-dir datasets/guo582
  python web_annotator/server.py --data-dir datasets/a --data-dir datasets/b
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from routes.alpha_paint import router as paint_router
from routes.annotations import router as annotations_router
from routes.aesthetic import router as aesthetic_router
from routes.booru import router as booru_router
from routes.crop import router as crop_router
from routes.dedupe import router as dedupe_router
from routes.docs import router as docs_router
from routes.images import router as images_router
from routes.meta import datasets_router, jobs_router
from routes.ready import router as ready_router
from routes.tag_stats import router as tag_stats_router
from routes.tagging import router as tagging_router
from services.dataset_layout import resolve_dataset_layout
from services.dataset_runtime import DatasetRegistry, DatasetRuntime
from services.jobs import JobManager
from services.scanner import scan_images
from services.state_manager import StateManager
from services.thumbnailer import Thumbnailer, default_workers
from services.work_dir import list_work_dirs


class _DevStaticFiles(StaticFiles):
    """开发期静态资源不缓存，避免改 JS 后浏览器仍用旧文件。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store"
        return resp


app = FastAPI(title="Image Annotator", version="3.0.0")

_PAGE_TEMPLATES = {
    "filter": "index.html",
    "aesthetic": "aesthetic.html",
    "dedupe": "dedupe.html",
    "crop": "crop.html",
    "paint": "paint.html",
    "tags": "tags.html",
    "tag-clean": "tag_clean.html",
    "ready": "ready.html",
}

_DS_PATH_RE = re.compile(r"^/(?:api/)?d/([^/]+)(?:/|$)")


def _migrate_meta_to_root(main_dir: Path, root: Path) -> None:
    """将误放在 main/ 下的元数据迁到数据集根。"""
    import shutil

    pairs = [
        (main_dir / ".thumbnails", root / ".thumbnails"),
        (main_dir / ".trash", root / ".trash"),
        (main_dir / ".annotator_state.json", root / ".annotator_state.json"),
    ]
    for old, new in pairs:
        if not old.exists():
            continue
        if not new.exists():
            shutil.move(str(old), str(new))
            print(f"已迁移: {old.name} → {root}/")
            continue
        if old.is_dir() and new.is_dir():
            for child in old.iterdir():
                dest = new / child.name
                if not dest.exists():
                    shutil.move(str(child), str(dest))
            shutil.rmtree(old, ignore_errors=True)
            print(f"已合并迁移: {old.name} → {root}/")
        elif old.is_file() and new.is_file():
            old.unlink(missing_ok=True)
            print(f"已清理重复: {old}")

    for nested in (main_dir / "aesthetic_report", root / "aesthetic_report"):
        html = nested / "aesthetic_report.html"
        dest = root / "aesthetic_report.html"
        if html.is_file() and not dest.exists():
            shutil.move(str(html), str(dest))
            print(f"已迁移: aesthetic_report.html → {root}/")
        if nested.is_dir():
            shutil.rmtree(nested, ignore_errors=True)


def _read_template(name: str) -> HTMLResponse:
    template = Path(__file__).parent / "templates" / name
    if not template.exists():
        return HTMLResponse(f"<h1>{name} not found</h1>", status_code=404)
    return HTMLResponse(template.read_text(encoding="utf-8"))


@app.middleware("http")
async def bind_dataset_middleware(request: Request, call_next):
    """
    从路径解析数据集名并写入 request.state.runtime。

    匹配 ``/api/d/{name}/...`` 与 ``/d/{name}/...``。
    引用：routes/deps.get_runtime。
    """
    m = _DS_PATH_RE.match(request.url.path)
    if m:
        name = m.group(1)
        reg: DatasetRegistry = request.app.state.datasets
        rt = reg.get(name)
        if rt is None:
            return JSONResponse({"detail": f"未知数据集: {name}"}, status_code=404)
        request.state.runtime = rt
    return await call_next(request)


static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", _DevStaticFiles(directory=str(static_dir)), name="static")

dataset_api = APIRouter(prefix="/api/d/{dataset}")
dataset_api.include_router(images_router)
dataset_api.include_router(annotations_router)
dataset_api.include_router(paint_router)
dataset_api.include_router(crop_router)
dataset_api.include_router(aesthetic_router)
dataset_api.include_router(tagging_router)
dataset_api.include_router(tag_stats_router)
dataset_api.include_router(dedupe_router)
dataset_api.include_router(ready_router)
dataset_api.include_router(booru_router)

app.include_router(dataset_api)
app.include_router(datasets_router)
app.include_router(jobs_router)
app.include_router(docs_router)


@app.get("/")
def home_page():
    """数据集入口页。引用：浏览器打开根路径。"""
    return _read_template("home.html")


@app.get("/d/{dataset}")
def dataset_root_redirect(dataset: str):
    return RedirectResponse(url=f"/d/{dataset}/filter", status_code=302)


@app.get("/d/{dataset}/thumb/{filename}")
def dataset_thumb(request: Request, dataset: str, filename: str):
    """
    缩略图文件。

    引用：前端 thumbnailUrl。
    """
    rt = request.app.state.datasets.get(dataset)
    if rt is None:
        raise HTTPException(404, f"未知数据集: {dataset}")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = rt.thumbnailer.thumb_dir / filename
    if not path.is_file():
        raise HTTPException(404, "缩略图不存在")
    return FileResponse(path, media_type="image/avif", headers={"Cache-Control": "no-store"})


@app.get("/d/{dataset}/{page}")
def dataset_page(dataset: str, page: str):
    """
    业务页面。路径中的 dataset 由中间件绑定。

    引用：nav.js 功能标签。
    """
    if page == "tagging":
        return RedirectResponse(url=f"/d/{dataset}/tags", status_code=302)
    tpl = _PAGE_TEMPLATES.get(page)
    if tpl is None:
        raise HTTPException(404, f"未知页面: {page}")
    return _read_template(tpl)

def _build_runtime(data_dir: Path, thumbnail_size: int, mask_dir_arg: str) -> DatasetRuntime:
    layout = resolve_dataset_layout(data_dir)
    _migrate_meta_to_root(layout.main_dir, layout.root)
    name = layout.root.name
    thumbnailer = Thumbnailer(layout.root, size=thumbnail_size, meta_dir=layout.root)
    state_manager = StateManager(layout.root, meta_dir=layout.root)
    n_mig = state_manager.migrate_flat_main_keys()
    if n_mig:
        print(f"[{name}] 已迁移旧路径键/回收站: {n_mig}")
    mask_dir = Path(mask_dir_arg).resolve() if mask_dir_arg else layout.mask_dir
    mask_dir.mkdir(parents=True, exist_ok=True)
    return DatasetRuntime(
        name=name,
        layout=layout,
        thumbnailer=thumbnailer,
        state_manager=state_manager,
        mask_dir=mask_dir,
    )


def _prewarm(rt: DatasetRuntime, images: list[dict]) -> None:
    from services.color_similarity import ColorIndex

    idx = ColorIndex(rt.thumbnailer.thumb_dir)
    rt.color_index = idx
    rels = [img["path"] for img in images]
    thumbs = {r: rt.thumbnailer.get_path(r) for r in rels}
    idx.ensure_many(rt.root, rels, thumbs)
    print(f"[{rt.name}] 色调索引就绪: {len(rels)} 张")


def main() -> int:
    parser = argparse.ArgumentParser(description="网页图片标注工具")
    parser.add_argument(
        "--data-dir",
        type=str,
        action="append",
        required=True,
        help="数据集根目录 datasets/xxx（可重复传入以挂载多个）",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--thumbnail-size", type=int, default=2560, help="缩略图长边像素")
    parser.add_argument("--skip-thumbnails", action="store_true", help="跳过缩略图预生成")
    parser.add_argument(
        "--mask-dir",
        type=str,
        default="",
        help="Alpha mask 目录（仅当只传入一个 --data-dir 时生效；默认各集 root/mask）",
    )
    parser.add_argument(
        "--default-crop",
        type=str,
        default="",
        metavar="WxH@Z",
        help="打开每张图时的默认裁切匹配，如 1152x1152@1.0",
    )
    parser.add_argument(
        "--booru-proxy",
        type=str,
        default="http://127.0.0.1:7890",
        help="Danbooru/Gelbooru 请求代理；空字符串表示直连",
    )
    args = parser.parse_args()

    registry = DatasetRegistry()
    for i, raw in enumerate(args.data_dir):
        mask_arg = args.mask_dir if (args.mask_dir and len(args.data_dir) == 1) else ""
        try:
            rt = _build_runtime(Path(raw), args.thumbnail_size, mask_arg)
            registry.add(rt)
        except (FileNotFoundError, ValueError) as e:
            print(f"错误: {e}")
            return 1

    app.state.datasets = registry
    app.state.jobs = JobManager()
    app.state.booru_proxy = (args.booru_proxy or "").strip() or None
    app.state.crop_default_tw = None
    app.state.crop_default_th = None
    app.state.crop_default_zoom = None
    app.state.paint_default_alpha = 255

    if args.default_crop:
        s = args.default_crop.strip().lower().rstrip("x")
        m = re.match(r"^(\d+)\s*x\s*(\d+)\s*@\s*([\d.]+)\s*x?$", s)
        if not m:
            print(f"错误: 无效 --default-crop 格式: {args.default_crop}")
            return 1
        app.state.crop_default_tw = int(m.group(1))
        app.state.crop_default_th = int(m.group(2))
        app.state.crop_default_zoom = float(m.group(3))

    for name in registry.names():
        rt = registry.get(name)
        images = []
        for d in list_work_dirs(rt.root):
            images.extend(scan_images(rt.root / d["name"], path_prefix=d["name"]))
        state = rt.state_manager.get()

        if not args.skip_thumbnails:
            missing = [img["path"] for img in images if not rt.thumbnailer.exists(img["path"])]
            if missing:
                workers = default_workers()
                print(f"[{name}] 预生成缩略图: {len(missing)} 张（并行 {workers}）...")

                def _thumb_progress(done: int, total: int, _n=name) -> None:
                    if done % 50 == 0 or done == total:
                        print(f"  [{_n}] {done}/{total}")

                rt.thumbnailer.generate_many(
                    missing, workers=workers, on_progress=_thumb_progress
                )
            else:
                print(f"[{name}] 缩略图已就绪")

        threading.Thread(target=_prewarm, args=(rt, images), daemon=True).start()

        part_names = [p.name for p in rt.layout.list_part_dirs()]
        print(f"数据集   : {name}")
        print(f"  根目录 : {rt.root}")
        print(f"  源图   : {rt.main_dir}")
        print(f"  Mask   : {rt.mask_dir}")
        print(f"  部位   : {', '.join(part_names) if part_names else '(无)'}")
        print(f"  图片   : {len(images)}")
        print(f"  已选   : {len(state.get('selected', {}))}")
        print(f"  回收站 : {len(state.get('trashed', {}))}")

    print(f"服务地址 : http://{args.host}:{args.port}")
    print("  首页选择数据集 → /d/{名称}/筛选|美学|去重|…")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
