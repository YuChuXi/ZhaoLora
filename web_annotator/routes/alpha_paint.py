"""
Alpha Paint API —— 提供原始尺寸 AVIF 图片，接收前端 mask PNG。
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image

from services.scanner import scan_images
from services.mask_manager import has_mask, load_mask, save_mask

router = APIRouter(prefix="/api/paint", tags=["paint"])


def _services(request: Request):
    return request.app.state.data_dir, request.app.state.mask_dir


@router.get("/images")
def list_paint_images(request: Request):
    """列出所有图片及 mask 状态。"""
    data_dir, mask_dir = _services(request)
    images = scan_images(data_dir)

    result = []
    for img in images:
        rel = img["path"]
        result.append({
            "path": rel,
            "stem": img["stem"],
            "has_mask": has_mask(mask_dir, rel),
        })
    result.sort(key=lambda x: (1 if x["has_mask"] else 0))
    return result


@router.get("/image")
def serve_original_image(request: Request, path: str = Query(...)):
    """
    提供原始尺寸的 AVIF 图片。
    前端通过 ?path=xxx 指定相对于 data_dir 的路径。
    """
    data_dir, _ = _services(request)
    src = data_dir / path
    if not src.exists():
        raise HTTPException(404, f"Image not found: {path}")

    img = Image.open(src).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="AVIF", quality=85)
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/avif")


@router.get("/mask")
def get_existing_mask(request: Request, path: str = Query(...)):
    """
    获取已保存的 mask PNG（灰度图）。
    返回 PNG 二进制，无 mask 则 404。
    """
    data_dir, mask_dir = _services(request)
    src = data_dir / path
    if not src.exists():
        raise HTTPException(404, "Image not found")

    rgb = Image.open(src)
    alpha = load_mask(mask_dir, path, rgb.size)
    if alpha is None:
        raise HTTPException(404, "No mask saved")
    buf = io.BytesIO()
    Image.fromarray(alpha, mode="L").save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")


@router.post("/upload-mask")
async def upload_mask(request: Request):
    """
    前端 POST 原始 PNG 二进制（灰度 alpha），Content-Type: image/png。
    Query: ?path=图片相对路径
    """
    data_dir, mask_dir = _services(request)
    path = request.query_params.get("path", "")
    if not path:
        raise HTTPException(400, "Missing 'path' query param")

    src = data_dir / path
    if not src.exists():
        raise HTTPException(404, f"Image not found: {path}")

    body = await request.body()
    mask_img = Image.open(io.BytesIO(body))
    if mask_img.mode not in ("L",):
        mask_img = mask_img.convert("L")
    alpha = np.array(mask_img, dtype=np.uint8)
    fmt = request.query_params.get("format", "alpha_bw")
    dest = save_mask(mask_dir, path, alpha, src, save_format=fmt)
    return {"saved": str(dest), "size": list(Image.open(src).size)}


# ---- 配置（服务器内存记忆） ----
@router.get("/config")
def get_config(request: Request):
    """获取 paint 配置（default_alpha 等）。"""
    da = getattr(request.app.state, 'paint_default_alpha', 255)
    return {"default_alpha": da}


@router.post("/config")
async def set_config(request: Request):
    """保存 paint 配置。"""
    body = await request.json()
    da = body.get("default_alpha", 255)
    request.app.state.paint_default_alpha = max(0, min(255, int(da)))
    return {"default_alpha": request.app.state.paint_default_alpha}
