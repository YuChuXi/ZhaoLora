"""裁剪 API：多选区吸附并输出到部位目录。"""

from __future__ import annotations

import io
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image

from services.scanner import scan_images
from services.crop_match import clamp_target_size, match_config
from services.dataset_layout import RESERVED_DIR_NAMES, DatasetLayout

router = APIRouter(prefix="/api/crop", tags=["crop"])

_SAFE_NAME = re.compile(r"^[\w\-]{1,64}$", re.UNICODE)


def _layout(request: Request) -> DatasetLayout:
    return request.app.state.layout


def _main_dir(request: Request) -> Path:
    return request.app.state.data_dir


def _safe_resolve(base: Path, rel: str) -> Path:
    src = (base / rel).resolve()
    if not str(src).startswith(str(base.resolve())):
        raise HTTPException(400, "非法路径")
    return src


def _require_part_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not _SAFE_NAME.match(name):
        raise HTTPException(400, "需要合法部位名（如 head / foot）")
    if name.lower() in RESERVED_DIR_NAMES:
        raise HTTPException(400, f"选区名称不能使用保留名: {name}")
    return name


def _resolve_out_path(layout: DatasetLayout, stem: str, part_name: str, index: int, total: int) -> Path:
    """
    part_name 非空且合法 → root/{part}/{stem}.png
    否则 → root/crops/{stem}.png 或 {stem}_001.png
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
def list_crop_images(request: Request):
    """列出 main 下图片及是否已有部位/裁剪输出。"""
    layout = _layout(request)
    images = scan_images(layout.main_dir)
    result = []
    for img in images:
        result.append({
            "path": img["path"],
            "stem": img["stem"],
            "done": layout.stem_has_part_output(img["stem"]),
        })
    result.sort(key=lambda x: (1 if x["done"] else 0, x["path"]))
    return result


@router.get("/image")
def serve_original_image(request: Request, path: str = Query(...)):
    """提供原图像素尺寸的 AVIF 预览（仅供框选；保存时仍读磁盘原图）。"""
    src = _safe_resolve(_main_dir(request), path)
    if not src.exists():
        raise HTTPException(404, f"Image not found: {path}")

    img = Image.open(src).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="AVIF", quality=85)
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/avif")


@router.get("/candidates")
def get_candidates():
    """返回边长网格配置（不再下发全量候选表）。"""
    return match_config()


@router.get("/config")
def get_config(request: Request):
    layout = _layout(request)
    tw = getattr(request.app.state, "crop_default_tw", None)
    th = getattr(request.app.state, "crop_default_th", None)
    z = getattr(request.app.state, "crop_default_zoom", None)
    return {
        "root": str(layout.root),
        "main_dir": str(layout.main_dir),
        "mask_dir": str(layout.mask_dir),
        "parts": [p.name for p in layout.list_part_dirs()],
        "default_crop": (
            {"target_w": tw, "target_h": th, "zoom": z}
            if tw and th and z
            else None
        ),
    }


@router.post("/save")
async def save_crops(request: Request):
    """
    body: {
      path: str,                 # 相对 main 的路径
      no_crop_resize: bool,      # 整图即某部位：原尺寸写入部位目录
      part_name: str,            # no_crop 时必填部位名
      crops: [{x1,y1,x2,y2,target_w,target_h,zoom,name?}, ...]
    }
    """
    layout = _layout(request)
    main_dir = layout.main_dir
    body = await request.json()
    rel = body.get("path", "")
    if not rel:
        raise HTTPException(400, "Missing path")

    src = _safe_resolve(main_dir, rel)
    if not src.exists():
        raise HTTPException(404, f"Image not found: {rel}")

    no_crop = bool(body.get("no_crop_resize", False))
    crops = body.get("crops") or []
    if not no_crop and not crops:
        raise HTTPException(400, "没有裁剪区域")

    img = Image.open(src).convert("RGBA")
    iw, ih = img.size
    stem = Path(rel).stem
    saved = []

    if no_crop:
        # 整图本身就是某个部位：原尺寸写入 xxx/{部位}/{stem}.png
        part = _require_part_name(
            body.get("part_name")
            or (crops[0].get("name") if crops else "")
            or ""
        )
        out_dir = layout.part_dir(part)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{stem}.png"
        img.save(out_path, format="PNG")
        saved.append({
            "file": str(out_path.relative_to(layout.root)),
            "size": [iw, ih],
            "name": part,
        })
        return {"saved": saved, "count": len(saved)}

    used_keys: set[str] = set()
    for i, c in enumerate(crops):
        x1 = int(c["x1"])
        y1 = int(c["y1"])
        x2 = int(c["x2"])
        y2 = int(c["y2"])
        x1 = max(0, min(iw - 1, x1))
        y1 = max(0, min(ih - 1, y1))
        x2 = max(x1 + 1, min(iw, x2))
        y2 = max(y1 + 1, min(ih, y2))
        cw, ch = x2 - x1, y2 - y1
        # 吸附结果以前端为准，后端只裁切 + 缩放到声明的 target
        tw = int(c.get("target_w") or cw)
        th = int(c.get("target_h") or ch)
        tw, th = clamp_target_size(tw, th)

        crop = img.crop((x1, y1, x2, y2))
        out_img = crop.resize((tw, th), Image.Resampling.LANCZOS)

        raw_name = (c.get("name") or "").strip()
        out_path = _resolve_out_path(layout, stem, raw_name, i, len(crops))

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
        saved.append({
            "file": str(out_path.relative_to(layout.root)),
            "crop": [x1, y1, x2, y2],
            "size": [tw, th],
            "name": raw_name or out_path.parent.name,
        })

    return {"saved": saved, "count": len(saved)}
