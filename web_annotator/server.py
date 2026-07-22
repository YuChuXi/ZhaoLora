#!/usr/bin/env python3
"""
网页图片标注工具 —— 瀑布流浏览，选择删除不需要的图片。

用法:
  python web_annotator/server.py --data-dir datasets/qianhu/main
  python web_annotator/server.py --data-dir datasets/tentacle_clothes/danbooru --port 8080

架构:
  routes/       - FastAPI 路由（images, annotations）
  services/     - 业务逻辑（scanner, thumbnailer, state_manager）
  templates/    - HTML 模板
  static/       - CSS / JS 静态资源
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
from routes.images import router as images_router
from routes.annotations import router as annotations_router
from routes.alpha_paint import router as paint_router

# ---------------------------------------------------------------------------
# FastAPI app —— 服务实例通过 app.state 注入，避免 __main__ vs 模块名问题
# ---------------------------------------------------------------------------
app = FastAPI(title="Image Annotator", version="2.0.0")

static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(images_router)
app.include_router(annotations_router)
app.include_router(paint_router)


@app.get("/")
def index():
    """主页面。"""
    template = Path(__file__).parent / "templates" / "index.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


@app.get("/paint")
def paint_page():
    """Alpha 绘制页面。"""
    template = Path(__file__).parent / "templates" / "paint.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>paint.html not found</h1>", status_code=404)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="网页图片标注工具 - 瀑布流删除")
    parser.add_argument("--data-dir", type=str, required=True, help="图片数据集目录")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--thumbnail-size", type=int, default=400, help="缩略图长边像素")
    parser.add_argument("--skip-thumbnails", action="store_true", help="跳过缩略图预生成")
    parser.add_argument("--mask-dir", type=str, default="", help="Alpha mask 输出目录（默认 data-dir/mask）")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"错误: 目录不存在: {data_dir}")
        return 1

    # 通过 app.state 注入服务
    app.state.data_dir = data_dir
    app.state.thumbnailer = Thumbnailer(data_dir, size=args.thumbnail_size)
    app.state.state_manager = StateManager(data_dir)
    # mask 输出目录（默认 data_dir/mask）
    mask_dir = Path(args.mask_dir) if args.mask_dir else (data_dir / "mask")
    mask_dir.mkdir(parents=True, exist_ok=True)
    app.state.mask_dir = mask_dir

    # 缩略图目录直接挂载为静态文件（不走 FastAPI 路由，性能等同 nginx）
    thumb_dir = app.state.thumbnailer.thumb_dir
    app.mount("/thumb", StaticFiles(directory=str(thumb_dir)), name="thumbs")

    images = scan_images(data_dir)
    state = app.state.state_manager.get()

    # ---- 批量预生成缩略图 ----
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

    print(f"数据目录 : {data_dir}")
    print(f"图片总数 : {len(images)}")
    print(f"已选待删 : {len(state.get('selected', {}))}")
    print(f"回收站内 : {len(state.get('trashed', {}))}")
    print(f"服务地址 : http://{args.host}:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
