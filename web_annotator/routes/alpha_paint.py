"""蒙版绘制 API. """

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from PIL import Image

from routes.deps import get_runtime, resolve_path
from services.core.scanner import scan_images
from services.state.mask import has_mask, load_mask, save_mask
from services.models import DefaultAlphaResponse, PaintImageItem, PaintSaveResponse

router = APIRouter(prefix="/paint", tags=["paint"])


class PaintConfigBody(BaseModel):
    """蒙版页内存配置. 引用: ``POST /paint/config``. """

    default_alpha: int = Field(255, ge=0, le=255)


def _services(request: Request) -> tuple[Path, Path]:
    """返回 ``(root, mask_dir)``. 引用: 本模块各接口. """
    rt = get_runtime(request)
    return rt.root, rt.mask_dir


@router.get("/images")
def list_paint_images(request: Request, dir: str = "main") -> list[PaintImageItem]:
    """列出指定子目录下图片及 mask 是否存在. 

    Args:
        dir: 子目录名, 默认 ``main``. 

    引用:
        ``paint.js``. 
    """
    root, mask_dir = _services(request)
    subdir = (dir or "main").strip().strip("/")
    images = scan_images(root / subdir, path_prefix=subdir)
    result: list[PaintImageItem] = []
    for img in images:
        result.append(
            PaintImageItem(
                path=img.path,
                stem=img.stem,
                has_mask=has_mask(mask_dir, img.path),
            )
        )
    result.sort(key=lambda x: (1 if x.has_mask else 0))
    return result


@router.get("/image")
def serve_original_image(request: Request, path: str = Query(...)) -> Response:
    """提供原图 AVIF 预览. 

    Args:
        path: 相对数据集根的路径(含子目录名). 

    引用:
        ``paint.js``. 
    """
    root, _ = _services(request)
    src = resolve_path(root, path)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {path}")
    img = Image.open(src).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="AVIF", quality=85)
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/avif")


@router.get("/mask")
def get_existing_mask(request: Request, path: str = Query(...)) -> Response:
    """返回已保存的灰度 mask PNG；不存在则 404. 

    Args:
        path: 相对数据集根的图片路径(含子目录名). 

    引用:
        ``paint.js``. 
    """
    root, mask_dir = _services(request)
    src = resolve_path(root, path)
    if not src.is_file():
        raise HTTPException(404, "Image not found")
    rgb = Image.open(src)
    alpha = load_mask(mask_dir, path, rgb.size)
    if alpha is None:
        raise HTTPException(404, "No mask saved")
    buf = io.BytesIO()
    # 下载只传蒙版灰度: 前端已下载过原图, mask 响应不再重复携带原图 RGB.
    # 上传受 canvas 约束走 A 通道; 下载最小化走灰度(L/R).
    Image.fromarray(alpha, mode="L").save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")


@router.post("/upload-mask")
async def upload_mask(
    request: Request,
    path: str = Query(...),
    format: str = Query("alpha_bw"),
) -> PaintSaveResponse:
    """接收灰度 PNG 并写入 mask 目录. 

    Args:
        path: 相对数据集根的图片路径(含子目录名). 
        format: ``alpha_bw`` 或 ``rgba``. 

    引用:
        ``paint.js``. 
    """
    root, mask_dir = _services(request)
    src = resolve_path(root, path)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {path}")
    body = await request.body()
    mask_img = Image.open(io.BytesIO(body))
    # 前端 canvas 导出 RGBA, 蒙版在 alpha 通道(笔刷绘制时 RGB 恒白),
    # 直接取 A 通道, 而非 convert("L") 的 RGB 亮度(否则绘制后保存必为纯白).
    alpha = np.array(mask_img.getchannel("A"), dtype=np.uint8)
    dest = save_mask(mask_dir, path, alpha, src, save_format=format)
    return PaintSaveResponse(saved=str(dest), size=list(Image.open(src).size))


@router.get("/config")
def get_config(request: Request) -> DefaultAlphaResponse:
    """读取蒙版页配置. 引用: ``paint.js``. """
    da = getattr(request.app.state, "paint_default_alpha", 255)
    return DefaultAlphaResponse(default_alpha=da)


@router.post("/config")
def set_config(request: Request, body: PaintConfigBody) -> DefaultAlphaResponse:
    """写入蒙版页配置. 引用: ``paint.js``. """
    request.app.state.paint_default_alpha = body.default_alpha
    return DefaultAlphaResponse(default_alpha=body.default_alpha)
