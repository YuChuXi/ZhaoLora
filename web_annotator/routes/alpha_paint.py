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
from services.core.thumbnailer import image_size
from services.state.mask import has_mask, load_mask, save_mask
from services.models import DefaultAlphaResponse, PaintImageItem, PaintSaveResponse

router = APIRouter(prefix="/paint", tags=["paint"])

# 蒙版页视觉预览长边上限(px): 预览仅用于展示, 压缩后可大幅降低传输量; 蒙版仍按原分辨率
_PAINT_PREVIEW_MAX = 2048


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
    thumbnailer = get_runtime(request).thumbnailer
    subdir = (dir or "main").strip().strip("/")
    images = scan_images(root / subdir, path_prefix=subdir)
    result: list[PaintImageItem] = []
    for img in images:
        w, h = image_size(root / img.path)
        result.append(
            PaintImageItem(
                path=img.path,
                stem=img.stem,
                has_mask=has_mask(mask_dir, img.path),
                width=w,
                height=h,
                thumbnail_hash=thumbnailer.hash_for(img.path),
            )
        )
    result.sort(key=lambda x: (1 if x.has_mask else 0))
    return result


@router.get("/image")
def serve_original_image(request: Request, path: str = Query(...)) -> Response:
    """提供原图 AVIF 预览(透明区域衬白底, 长边压缩到 _PAINT_PREVIEW_MAX). 

    原图尺寸由 ``/paint/images`` 列表携带, 前端据此按原分辨率绘制蒙版. 

    Args:
        path: 相对数据集根的路径(含子目录名). 

    引用:
        ``paint.js``. 
    """
    root, _ = _services(request)
    src = resolve_path(root, path)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {path}")
    rgba = Image.open(src).convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.split()[3])
    if max(bg.size) > _PAINT_PREVIEW_MAX:
        bg.thumbnail((_PAINT_PREVIEW_MAX, _PAINT_PREVIEW_MAX), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, format="AVIF", quality=80)
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/avif")


@router.get("/mask")
def get_existing_mask(request: Request, path: str = Query(...)) -> Response:
    """返回原始蒙版(蒙版置入 A 通道的无损 PNG, RGB 恒白)；不存在或尺寸不符则 404. 

    Args:
        path: 相对数据集根的图片路径(含子目录名). 

    引用:
        ``paint.js``. 
    """
    root, mask_dir = _services(request)
    src = resolve_path(root, path)
    if not src.is_file():
        raise HTTPException(404, "Image not found")
    alpha = load_mask(mask_dir, path, Image.open(src).size)
    if alpha is None:
        raise HTTPException(404, "No mask saved")
    h, w = alpha.shape
    white = Image.new("L", (w, h), 255)
    mask_img = Image.merge("RGBA", (white, white, white, Image.fromarray(alpha, mode="L")))
    buf = io.BytesIO()
    mask_img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")


@router.post("/upload-mask")
async def upload_mask(request: Request, path: str = Query(...)) -> PaintSaveResponse:
    """接收蒙版 RGBA PNG 并写入 mask 目录(取 A 通道). 

    Args:
        path: 相对数据集根的图片路径(含子目录名). 

    引用:
        ``paint.js``. 
    """
    root, mask_dir = _services(request)
    src = resolve_path(root, path)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {path}")
    body = await request.body()
    mask_img = Image.open(io.BytesIO(body))
    # 前端 canvas 导出 RGBA, 蒙版在 alpha 通道(笔刷绘制时 RGB 恒黑),
    # 直接取 A 通道, 而非 convert("L") 的 RGB 亮度(否则绘制后保存必为纯色).
    alpha = np.array(mask_img.getchannel("A"), dtype=np.uint8)
    save_mask(mask_dir, path, alpha)
    # saved 返回相对数据集根的 mask 路径(镜像原图路径), 不暴露绝对路径
    return PaintSaveResponse(saved=str(Path(path).with_suffix(".png")), size=list(Image.open(src).size))


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
