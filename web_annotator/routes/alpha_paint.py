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
from services.state.mask import has_mask, load_mask, mask_out_path, save_mask
from services.models import DefaultAlphaResponse, PaintImageItem, PaintSaveResponse

router = APIRouter(prefix="/paint", tags=["paint"])

# 蒙版页视觉预览长边上限(px): 预览仅用于展示, 压缩后可大幅降低传输量; 蒙版仍按原分辨率
_PAINT_PREVIEW_MAX = 2048


class PaintConfigBody(BaseModel):
    """蒙版页默认 alpha 配置请求体. """

    default_alpha: int = Field(255, ge=0, le=255)


def _services(request: Request) -> tuple[Path, Path]:
    """获取当前数据集的根目录与蒙版目录路径.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.

    Returns:
        tuple[Path, Path]: ``(root, mask_dir)``, 数据集根与 mask 目录绝对路径.
    Callers:
        - ``routes/alpha_paint.py:get_existing_mask``
        - ``routes/alpha_paint.py:list_paint_images``
        - ``routes/alpha_paint.py:serve_mask_preview``
        - ``routes/alpha_paint.py:serve_original_image``
        - ``routes/alpha_paint.py:upload_mask``
    """
    rt = get_runtime(request)
    return rt.root, rt.mask_dir


@router.get("/images")
def list_paint_images(request: Request, dir: str = "main") -> list[PaintImageItem]:
    """列出指定子目录下图片及 mask 是否存在.

    按 has_mask 升序排序(未绘制在前), 便于切帧.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        dir (str): 子目录名, 默认 ``main``.

    Returns:
        list[PaintImageItem]: 图片清单, 含路径/stem/尺寸/缩略图哈希/是否有蒙板.

    Callers:
        - ``static/js/paint.js:loadImages``
        - ``static/js/paint.js:refreshAfterMaskDone``
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
        request (Request): FastAPI 请求对象, 由路由参数注入.
        path (str): 相对数据集根的图片路径(含子目录名).

    Returns:
        Response: ``image/avif`` 响应体, 长边不超过 _PAINT_PREVIEW_MAX.

    Raises:
        HTTPException: 图片不存在时返回 404.

    Callers:
        - ``static/js/paint.js:_loadEntry`` (经 ``_fetchBlob('/paint/image', ...)``)
        - ``routes/alpha_paint.py:serve_mask_preview`` (无蒙板时退回此接口)
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
    """返回原始蒙版(蒙版置入 A 通道的无损 PNG, RGB 恒白); 不存在或尺寸不符则 404.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        path (str): 相对数据集根的图片路径(含子目录名).

    Returns:
        Response: ``image/png`` 响应体, RGBA(A=蒙版, RGB=255).

    Raises:
        HTTPException: 原图不存在或蒙版未保存/尺寸不符时返回 404.

    Callers:
        - ``static/js/paint.js:_loadEntry`` (经 ``_fetchBlob('/paint/mask', ...)``)
        - ``static/js/paint.js:reloadCurrentMask`` (经 ``_fetchBlob('/paint/mask', ...)``)
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


def compose_mask_preview(base: Path, alpha: np.ndarray, max_size: int = _PAINT_PREVIEW_MAX) -> bytes:
    """把底图按蒙板 alpha 合成为透明抠图并编码为 AVIF 字节.

    输出 RGBA: 底图 RGB 保留, 蒙板写入 alpha 通道; 透明区由前端 CSS 棋盘格背景透出,
    因此不在此烘焙棋盘格, 棋盘格随屏幕固定大小显示、不随图片缩放。

    Args:
        base (Path): 底图绝对路径(优先缩略图, 无缩略图时用原图).
        alpha (np.ndarray): 蒙板灰度数组 ``(h, w)``, 0-255; 尺寸与底图不一致时缩放到底图尺寸.
        max_size (int): 输出长边上限.

    Returns:
        bytes: AVIF 编码的透明抠图字节.

    Callers:
        - ``routes/alpha_paint.py:serve_mask_preview``
    """
    rgba = Image.open(base).convert("RGBA")
    if alpha.shape[::-1] != rgba.size:
        alpha = np.array(
            Image.fromarray(alpha, mode="L").resize(rgba.size, Image.Resampling.LANCZOS),
            dtype=np.uint8,
        )
    rgba.putalpha(Image.fromarray(alpha, mode="L"))
    if max(rgba.size) > max_size:
        rgba.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    rgba.save(buf, format="AVIF", quality=80)
    buf.seek(0)
    return buf.getvalue()


@router.get("/mask-preview")
def serve_mask_preview(request: Request, path: str = Query(...)) -> Response:
    """返回应用蒙板后的透明抠图: 底图 RGB + 蒙板 alpha, AVIF 编码.

    优先使用缩略图作为底图; 合成结果缓存到缩略图目录(``<hash>_mask.avif``),
    缓存命中时直接返回已编码字节, 绕过解码-编码循环; 底图或蒙板被修改后
    自动失效重生成。无缩略图时退回原图; 无蒙板时退回原图预览(白底)。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        path (str): 相对数据集根的图片路径(含子目录名).

    Returns:
        Response: ``image/avif`` 响应体, 合成后的透明抠图。

    Raises:
        HTTPException: 原图不存在时返回 404。

    Callers:
        - ``static/js/tag_check.js:imageSrc``
    """
    root, mask_dir = _services(request)
    thumbnailer = get_runtime(request).thumbnailer
    src = resolve_path(root, path)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {path}")
    with Image.open(src) as im:
        size = im.size
    alpha = load_mask(mask_dir, path, size)
    if alpha is None:
        return serve_original_image(request, path=path)
    base = thumbnailer.get_path(path)
    if not base.is_file():
        base = src
    cache_path = thumbnailer.thumb_dir / f"{thumbnailer.hash_for(path)}_mask.avif"
    mask_file = mask_out_path(mask_dir, path)
    base_mtime = base.stat().st_mtime
    mask_mtime = mask_file.stat().st_mtime
    if cache_path.is_file() and cache_path.stat().st_mtime >= max(base_mtime, mask_mtime):
        return Response(content=cache_path.read_bytes(), media_type="image/avif")
    data = compose_mask_preview(base, alpha)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return Response(content=data, media_type="image/avif")


@router.post("/upload-mask")
async def upload_mask(request: Request, path: str = Query(...)) -> PaintSaveResponse:
    """接收蒙版 RGBA PNG 并写入 mask 目录(取 A 通道).

    前端 canvas 导出 RGBA, 蒙版在 alpha 通道(笔刷绘制时 RGB 恒黑),
    直接取 A 通道, 而非 convert("L") 的 RGB 亮度(否则绘制后保存必为纯色).

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入, body 为 RGBA PNG 字节.
        path (str): 相对数据集根的图片路径(含子目录名).

    Returns:
        PaintSaveResponse: 含相对数据集根的 mask 路径与原图尺寸.

    Raises:
        HTTPException: 原图不存在时返回 404.

    Callers:
        - ``static/js/paint.js:doSave``
    """
    root, mask_dir = _services(request)
    src = resolve_path(root, path)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {path}")
    body = await request.body()
    mask_img = Image.open(io.BytesIO(body))
    alpha = np.array(mask_img.getchannel("A"), dtype=np.uint8)
    save_mask(mask_dir, path, alpha)
    return PaintSaveResponse(saved=str(Path(path).with_suffix(".png")), size=list(Image.open(src).size))


@router.get("/config")
def get_config(request: Request) -> DefaultAlphaResponse:
    """读取蒙版页默认 alpha 配置.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.

    Returns:
        DefaultAlphaResponse: 当前默认 alpha 值(0-255, 未设置时为 255).

    Callers:
        - ``static/js/paint.js:loadConfig``
    """
    da = getattr(request.app.state, "paint_default_alpha", 255)
    return DefaultAlphaResponse(default_alpha=da)


@router.post("/config")
def set_config(request: Request, body: PaintConfigBody) -> DefaultAlphaResponse:
    """写入蒙版页默认 alpha 配置(仅存于内存的 app.state).

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (PaintConfigBody): 含待写入的 default_alpha(0-255).

    Returns:
        DefaultAlphaResponse: 写入后的最新默认 alpha 值.

    Callers:
        - ``static/js/paint.js:saveConfig``
    """
    request.app.state.paint_default_alpha = body.default_alpha
    return DefaultAlphaResponse(default_alpha=body.default_alpha)
