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
from services.core.thumbnailer import image_size
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
    """单个裁剪框. """

    x1: int
    y1: int
    x2: int
    y2: int
    target_w: int | None = None
    target_h: int | None = None
    zoom: float | None = None
    name: str = ""


class SaveCropsBody(BaseModel):
    """裁剪保存请求体. """

    path: str = Field(..., min_length=1)
    no_crop_resize: bool = False
    part_name: str = ""
    crops: list[CropRect] = Field(default_factory=list)


def _layout(request: Request) -> DatasetLayout:
    """获取当前数据集布局对象.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.

    Returns:
        DatasetLayout: 当前数据集布局, 用于定位 main/crops/部位目录.
    """
    return get_runtime(request).layout


def _main_dir(request: Request) -> Path:
    """获取当前数据集主目录(main)路径.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.

    Returns:
        Path: main 目录绝对路径.
    """
    return get_runtime(request).main_dir


def _require_part_name(name: str) -> str:
    """校验部位目录名, 非法或为保留名时抛出 HTTPException.

    Args:
        name (str): 候选部位名, 允许空白会被规整处理.

    Returns:
        str: 规整后的合法部位名.

    Raises:
        HTTPException: 名称为空、不匹配命名规则或占用保留名时抛出.
    """
    name = (name or "").strip()
    if not name or not _SAFE_NAME.match(name):
        raise HTTPException(400, "需要合法部位名(如 head / foot)")
    if name.lower() in RESERVED_DIR_NAMES:
        raise HTTPException(400, f"选区名称不能使用保留名: {name}")
    return name


def _resolve_out_path(layout: DatasetLayout, stem: str, part_name: str, index: int, total: int) -> Path:
    """计算单张裁剪的输出路径.

    有合法 ``part_name`` 时写入 ``root/{part}/{stem}.png``;
    否则写入 ``root/crops/``, 多张时附加序号 ``_{index+1:03d}``.

    Args:
        layout (DatasetLayout): 当前数据集布局, 用于定位部位目录与 crops 目录.
        stem (str): 源图文件名 stem, 用作输出文件名前缀.
        part_name (str): 候选部位名, 非空且合法时归入对应部位目录.
        index (int): 当前选区在保存序列中的索引(用于多张时编号).
        total (int): 本轮保存的选区总数(决定是否需要附加序号).

    Returns:
        Path: 输出 PNG 文件绝对路径.

    Raises:
        HTTPException: 部位名占用保留名时抛出.
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

    按 (是否已完成, 路径名) 升序排序, 未完成项排在前面便于切帧.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.

    Returns:
        list[CropImageItem]: main 下图片清单, 含路径/stem/尺寸/缩略图哈希/完成标记.

    Callers:
        - ``static/js/crop.js:loadImages``
    """
    layout = _layout(request)
    thumbnailer = get_runtime(request).thumbnailer
    images = scan_images(layout.main_dir)
    result: list[CropImageItem] = []
    for img in images:
        rel = f"main/{img.path}"
        w, h = image_size(layout.main_dir / img.path)
        result.append(
            CropImageItem(
                path=img.path,
                stem=img.stem,
                done=layout.stem_has_part_output(img.stem),
                thumbnail_hash=thumbnailer.hash_for(rel),
                width=w,
                height=h,
            )
        )
    result.sort(key=lambda x: (1 if x.done else 0, x.path))
    return result


@router.get("/image")
def serve_original_image(request: Request, path: str = Query(...)) -> Response:
    """提供原图像素尺寸的 AVIF 预览(仅供框选; 保存时仍读磁盘原图).

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        path (str): main 目录内图片相对路径.

    Returns:
        Response: ``image/avif`` 响应体, 像素与磁盘原图一致.

    Raises:
        HTTPException: 图片不存在时返回 404.

    Callers:
        - ``static/js/crop.js:loadIndex``
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
    """返回边长网格配置, 供前端按网格规格吸附选区.

    Returns:
        CropMatchConfig: 候选边长网格配置.

    Callers:
        - ``static/js/crop.js:init``
    """
    return match_config()


@router.get("/config")
def get_config(request: Request) -> CropPageConfig:
    """裁剪页配置, 含已存在的部位目录列表(默认裁切由前端 localStorage 管理).

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.

    Returns:
        CropPageConfig: 含已存在的部位目录名列表.

    Callers:
        - ``static/js/crop.js:init``
    """
    layout = _layout(request)
    return CropPageConfig(
        parts=[p.name for p in layout.list_part_dirs()],
    )


@router.post("/save")
def save_crops(request: Request, body: SaveCropsBody) -> CropSaveResponse:
    """按框裁切并写入部位目录或 ``crops/``.

    当 ``body.no_crop_resize`` 为真时整图直接保存到部位目录;
    否则逐个选区裁切, 缩放到目标尺寸, 写入部位目录或 ``crops/``,
    同名输出文件自动追加序号避免覆盖.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        body (SaveCropsBody): 保存参数, 含源图路径/是否整图输出/部位名/选区列表.

    Returns:
        CropSaveResponse: 保存结果, 含每个输出文件的相对路径/裁剪框/尺寸/部位名.

    Raises:
        HTTPException: 图片不存在(404); 无裁剪区域或部位名非法(400).

    Callers:
        - ``static/js/crop.js:saveAndNext``
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
