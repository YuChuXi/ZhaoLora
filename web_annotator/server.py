#!/usr/bin/env python3
"""
网页图片标注工具。

目录约定（--data-dir = datasets/xxx）:
  datasets/xxx/
    main/     # 源图
    mask/     # alpha mask
    head/     # 部位裁剪（选区命名为 head 时写入；整图即部位时同）
    foot/     # 其它部位同理
    crops/    # 未命名选区兜底

用法:
  python web_annotator/server.py --data-dir datasets/guo582
  python web_annotator/server.py --data-dir datasets/guo582 --port 8080
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn

from services.scanner import scan_images
from services.thumbnailer import Thumbnailer
from services.state_manager import StateManager
from services.dataset_layout import resolve_dataset_layout
from routes.images import router as images_router
from routes.annotations import router as annotations_router
from routes.alpha_paint import router as paint_router
from routes.crop import router as crop_router
from routes.tag_stats import router as tag_stats_router
from routes.tagging import router as tagging_router
from routes.aesthetic import router as aesthetic_router
from routes.dedupe import router as dedupe_router
from routes.ready import router as ready_router

app = FastAPI(title="Image Annotator", version="2.2.0")


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
        # 目标已有：合并目录内容后删除旧目录
        if old.is_dir() and new.is_dir():
            for child in old.iterdir():
                dest = new / child.name
                if not dest.exists():
                    shutil.move(str(child), str(dest))
            shutil.rmtree(old, ignore_errors=True)
            print(f"已合并迁移: {old.name} → {root}/")
        elif old.is_file() and new.is_file():
            # 保留根目录已有状态，删除 main 下旧文件避免双份
            old.unlink(missing_ok=True)
            print(f"已清理重复: {old}")

    # 旧版套层目录 → 根目录单文件
    for nested in (main_dir / "aesthetic_report", root / "aesthetic_report"):
        html = nested / "aesthetic_report.html"
        dest = root / "aesthetic_report.html"
        if html.is_file() and not dest.exists():
            shutil.move(str(html), str(dest))
            print(f"已迁移: aesthetic_report.html → {root}/")
        if nested.is_dir():
            shutil.rmtree(nested, ignore_errors=True)

static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(images_router)
app.include_router(annotations_router)
app.include_router(paint_router)
app.include_router(crop_router)
app.include_router(aesthetic_router)
app.include_router(tagging_router)
app.include_router(tag_stats_router)
app.include_router(dedupe_router)
app.include_router(ready_router)


@app.get("/")
def index():
    template = Path(__file__).parent / "templates" / "index.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


@app.get("/paint")
def paint_page():
    template = Path(__file__).parent / "templates" / "paint.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>paint.html not found</h1>", status_code=404)


@app.get("/crop")
def crop_page():
    template = Path(__file__).parent / "templates" / "crop.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>crop.html not found</h1>", status_code=404)


@app.get("/aesthetic")
def aesthetic_page():
    template = Path(__file__).parent / "templates" / "aesthetic.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>aesthetic.html not found</h1>", status_code=404)


@app.get("/tagging")
def tagging_page():
    template = Path(__file__).parent / "templates" / "tagging.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>tagging.html not found</h1>", status_code=404)


@app.get("/tags")
def tags_page():
    template = Path(__file__).parent / "templates" / "tags.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>tags.html not found</h1>", status_code=404)


@app.get("/ready")
def ready_page():
    template = Path(__file__).parent / "templates" / "ready.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>ready.html not found</h1>", status_code=404)


@app.get("/dedupe")
def dedupe_page():
    template = Path(__file__).parent / "templates" / "dedupe.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>dedupe.html not found</h1>", status_code=404)


def main() -> int:
    parser = argparse.ArgumentParser(description="网页图片标注工具")
    parser.add_argument(
        "--data-dir",
        type=str,
        required=True,
        help="数据集根目录 datasets/xxx（图像在 xxx/main，mask/部位为同级目录）",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--thumbnail-size", type=int, default=2560, help="缩略图长边像素")
    parser.add_argument("--skip-thumbnails", action="store_true", help="跳过缩略图预生成")
    parser.add_argument(
        "--mask-dir",
        type=str,
        default="",
        help="Alpha mask 目录（默认 data-dir/mask）",
    )
    parser.add_argument(
        "--default-crop",
        type=str,
        default="",
        metavar="WxH@Z",
        help="打开每张图时的默认裁切匹配，如 1152x1152@1.0",
    )
    args = parser.parse_args()

    try:
        layout = resolve_dataset_layout(Path(args.data_dir))
    except (FileNotFoundError, ValueError) as e:
        print(f"错误: {e}")
        return 1

    # data_dir = main（裁剪/蒙版）；dataset_root = 根（筛选路径 / 缩略图 / 回收站）
    _migrate_meta_to_root(layout.main_dir, layout.root)

    app.state.layout = layout
    app.state.dataset_root = layout.root
    app.state.data_dir = layout.main_dir
    app.state.thumbnailer = Thumbnailer(
        layout.root,
        size=args.thumbnail_size,
        meta_dir=layout.root,
    )
    app.state.state_manager = StateManager(layout.root, meta_dir=layout.root)
    n_mig = app.state.state_manager.migrate_flat_main_keys()
    if n_mig:
        print(f"已迁移旧路径键/回收站: {n_mig}")

    mask_dir = Path(args.mask_dir).resolve() if args.mask_dir else layout.mask_dir
    mask_dir.mkdir(parents=True, exist_ok=True)
    app.state.mask_dir = mask_dir

    app.state.crop_default_tw = None
    app.state.crop_default_th = None
    app.state.crop_default_zoom = None
    if args.default_crop:
        import re

        s = args.default_crop.strip().lower().rstrip("x")
        m = re.match(r"^(\d+)\s*x\s*(\d+)\s*@\s*([\d.]+)\s*x?$", s)
        if not m:
            print(f"错误: 无效 --default-crop 格式: {args.default_crop}")
            return 1
        app.state.crop_default_tw = int(m.group(1))
        app.state.crop_default_th = int(m.group(2))
        app.state.crop_default_zoom = float(m.group(3))

    thumb_dir = app.state.thumbnailer.thumb_dir
    app.mount("/thumb", StaticFiles(directory=str(thumb_dir)), name="thumbs")

    images = scan_images(layout.main_dir, path_prefix="main")
    state = app.state.state_manager.get()

    if not args.skip_thumbnails:
        missing = [img for img in images if not app.state.thumbnailer.exists(img["path"])]
        if missing:
            print(f"预生成缩略图: {len(missing)} 张...")
            for i, img in enumerate(missing):
                app.state.thumbnailer.generate(img["path"])
                if (i + 1) % 50 == 0 or i == len(missing) - 1:
                    print(f"  {i + 1}/{len(missing)}")
        else:
            print("缩略图缓存完整，跳过预生成")

    # 后台预热色调索引（用缩略图，不挡启动）
    def _prewarm_color():
        from services.color_similarity import ColorIndex
        idx = ColorIndex(app.state.thumbnailer.thumb_dir)
        app.state.color_index = idx
        rels = [img["path"] for img in images]
        thumbs = {r: app.state.thumbnailer.get_path(r) for r in rels}
        idx.ensure_many(layout.root, rels, thumbs)
        print(f"色调索引就绪: {len(rels)} 张")

    import threading
    threading.Thread(target=_prewarm_color, daemon=True).start()

    part_names = [p.name for p in layout.list_part_dirs()]
    print(f"数据集根 : {layout.root}")
    print(f"源图目录 : {layout.main_dir}")
    print(f"Mask目录 : {mask_dir}")
    print(f"部位目录 : {', '.join(part_names) if part_names else '(无)'}")
    print(f"图片总数 : {len(images)}")
    print(f"已选待删 : {len(state.get('selected', {}))}")
    print(f"回收站内 : {len(state.get('trashed', {}))}")
    print(f"服务地址 : http://{args.host}:{args.port}")
    print(f"  筛选 / 去重 / 裁剪 / 蒙版 / 美学 / 打标 / 标签 / 就绪")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
