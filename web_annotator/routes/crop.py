"""裁剪 API: 多选区吸附并输出到部位目录. """

from __future__ import annotations

import io
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from PIL import Image

from routes.deps import get_runtime, resolve_path
from services.core.scanner import scan_images
from services.dataset.crop import clamp_target_size, match_config
from services.dataset.layout import RESERVED_DIR_NAMES, DatasetLayout
from services.models import (
    CropImageItem,
    CropMatchConfig,
    CropPageConfig,
    CropSaveResponse,
    CropSavedItem,
)

router = APIRouter(prefix="/crop", tags=["crop"])

_SAFE_NAME = re.compile(r"^[\w\-]{1,64}$", re.UNICODE)


class CropRect(BaseModel):
    """单个裁剪框. 引用: ``SaveCropsBody``. """

    x1: int
    y1: int
    x2: int
    y2: int
    target_w: int | None = None
    target_h: int | None = None
    zoom: float | None = None
    name: str = ""


class SaveCropsBody(BaseModel):
    """裁剪保存请求体. 引用: ``POST /crop/save``、``crop.js``. """

    path: str = Field(..., min_length=1)
    no_crop_resize: bool = False
    part_name: str = ""
    crops: list[CropRect] = Field(default_factory=list)


def _layout(request: Request) -> DatasetLayout:
    """引用: 本模块各接口. """
    return get_runtime(request).layout


def _main_dir(request: Request) -> Path:
    """引用: ``serve_original_image`` / ``save_crops``. """
    return get_runtime(request).main_dir


def _require_part_name(name: str) -> str:
    """校验部位目录名. 

    Raises:
        HTTPException: 名称非法或为保留名. 

    引用:
        ``save_crops``. 
    """
    name = (name or "").strip()
    if not name or not _SAFE_NAME.match(name):
        raise HTTPException(400, "需要合法部位名(如 head / foot)")
    if name.lower() in RESERVED_DIR_NAMES:
        raise HTTPException(400, f"选区名称不能使用保留名: {name}")
    return name


def _resolve_out_path(layout: DatasetLayout, stem: str, part_name: str, index: int, total: int) -> Path:
    """计算裁剪输出路径. 

    有合法 ``part_name`` 时写入 ``root/{part}/{stem}.png``；
    否则写入 ``root/crops/``. 

    引用:
        ``save_crops``. 
    """
    if part_name and _SAFE_NAME.match(part_name):
        if part_name.lower() in RESERVED_DIR_NAMES:
            raise HTTPException(400, f"选区名称不能使用保留名: {part_name}")
        out_dir = layout.part_dir(part_name)
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{stem}.png"

    layout.crops_dir.mkdir(parents=True, exist_ok=True)
    if total == 1:
        return layout.crops_dir / f"{stem}.png"
    return layout.crops_dir / f"{stem}_{index + 1:03d}.png"


@router.get("/images")
def list_crop_images(request: Request) -> list[CropImageItem]:
    """列出 main 下图片及是否已有部位或裁剪输出. 

    引用:
        ``crop.js``. 
    """
    layout = _layout(request)
    images = scan_images(layout.main_dir)
    result: list[CropImageItem] = []
    for img in images:
        result.append(
            CropImageItem(
                path=img.path,
                stem=img.stem,
                done=layout.stem_has_part_output(img.stem),
            )
        )
    result.sort(key=lambda x: (1 if x.done else 0, x.path))
    return result


@router.get("/image")
def serve_original_image(request: Request, path: str = Query(...)) -> Response:
    """提供原图像素尺寸的 AVIF 预览(仅供框选；保存时仍读磁盘原图). 

    引用:
        ``crop.js``. 
    """
    src = resolve_path(_main_dir(request), path)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {path}")

    img = Image.open(src).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="AVIF", quality=85)
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/avif")


@router.get("/candidates")
def get_candidates() -> CropMatchConfig:
    """返回边长网格配置. 

    引用:
        ``crop.js``. 
    """
    return match_config()


@router.get("/config")
def get_config(request: Request) -> CropPageConfig:
    """
    裁剪页配置. 默认裁切由前端 localStorage 管理, 不再下发服务端默认. 

    引用: crop.js. 
    """
    layout = _layout(request)
    return CropPageConfig(
        root=str(layout.root),
        main_dir=str(layout.main_dir),
        mask_dir=str(layout.mask_dir),
        parts=[p.name for p in layout.list_part_dirs()],
    )


@router.post("/save")
def save_crops(request: Request, body: SaveCropsBody) -> CropSaveResponse:
    """按框裁切并写入部位目录或 ``crops/``. 

    引用:
        ``crop.js``. 
    """
    layout = _layout(request)
    main_dir = layout.main_dir
    rel = body.path
    src = resolve_path(main_dir, rel)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {rel}")

    if not body.no_crop_resize and not body.crops:
        raise HTTPException(400, "没有裁剪区域")

    img = Image.open(src).convert("RGBA")
    iw, ih = img.size
    stem = Path(rel).stem
    saved: list[CropSavedItem] = []

    if body.no_crop_resize:
        part = _require_part_name(
            body.part_name
            or (body.crops[0].name if body.crops else "")
            or ""
        )
        out_dir = layout.part_dir(part)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{stem}.png"
        img.save(out_path, format="PNG")
        saved.append(
            CropSavedItem(
                file=str(out_path.relative_to(layout.root)),
                size=[iw, ih],
                name=part,
            )
        )
        return CropSaveResponse(saved=saved, count=len(saved))

    used_keys: set[str] = set()
    for i, c in enumerate(body.crops):
        x1 = max(0, min(iw - 1, c.x1))
        y1 = max(0, min(ih - 1, c.y1))
        x2 = max(x1 + 1, min(iw, c.x2))
        y2 = max(y1 + 1, min(ih, c.y2))
        cw, ch = x2 - x1, y2 - y1
        tw = int(c.target_w or cw)
        th = int(c.target_h or ch)
        tw, th = clamp_target_size(tw, th)

        crop = img.crop((x1, y1, x2, y2))
        out_img = crop.resize((tw, th), Image.Resampling.LANCZOS)

        raw_name = (c.name or "").strip()
        out_path = _resolve_out_path(layout, stem, raw_name, i, len(body.crops))

        key = str(out_path)
        n = 1
        while key in used_keys:
            if raw_name and _SAFE_NAME.match(raw_name):
                out_path = layout.part_dir(raw_name) / f"{stem}_{n:02d}.png"
            else:
                out_path = layout.crops_dir / f"{stem}_{i + 1:03d}_{n:02d}.png"
            key = str(out_path)
            n += 1
            if n > 99:
                break

        used_keys.add(key)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_img.save(out_path, format="PNG")
        saved.append(
            CropSavedItem(
                file=str(out_path.relative_to(layout.root)),
                crop=[x1, y1, x2, y2],
                size=[tw, th],
                name=raw_name or out_path.parent.name,
            )
        )

    return CropSaveResponse(saved=saved, count=len(saved))
