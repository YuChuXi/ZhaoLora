"""裁剪 API: 多选区吸附并输出到部位目录. """

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from PIL import Image

from routes.deps import DatasetDep, resolve_path
from services.core.atomic import atomic_write_text
from services.core.thumbnailer import image_size
from services.dataset.crop import clamp_target_size, match_config
from services.dataset.runtime import Dataset
from services.dataset.workdir import RESERVED_DIR_NAMES, VIDEO_DIRNAME, WorkDir
from services.models import (
    CropImageState,
    CropMatchConfig,
    CropPageConfig,
    CropPartRecord,
    CropSaveResponse,
    CropSavedItem,
    ImageThumbItem,
)

logger = logging.getLogger(__name__)

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
    """裁剪保存请求体. ``path`` 为数据集根相对路径(如 ``main/foo.png``). """

    path: str = Field(..., min_length=1)
    no_crop_resize: bool = False
    part_name: str = ""
    crops: list[CropRect] = Field(default_factory=list)


def _out_dir_for(ds: Dataset, name: str, source: WorkDir) -> Path:
    """校验并解析命名输出的目录(根下 ``root/{name}/``, 不存在则创建).

    输出目录名即部位名, 须合法且不得占用保留名(main/mask/crops 等)与视频
    目录 video, 也不得与当前源目录相同, 防止裁剪结果写入 main/video 或源图
    目录造成混写.

    Args:
        ds (Dataset): 当前数据集, 用于定位输出目录.
        name (str): 候选输出目录名(部位名), 允许空白由本校验拒绝.
        source (WorkDir): 源图所在工作目录; 其前缀参与同名校验.

    Returns:
        Path: 输出目录绝对路径(已创建).

    Raises:
        HTTPException: 名称为空、不匹配命名规则、占用保留名或与源目录相同时抛出.

    Callers:
        - ``routes/crop.py:save_crops``
        - ``routes/crop.py:_resolve_out_path``
    """
    name = (name or "").strip()
    if not name or not _SAFE_NAME.match(name):
        raise HTTPException(400, "需要合法部位名(如 head / foot)")
    if name.lower() in RESERVED_DIR_NAMES:
        raise HTTPException(400, f"输出目录不能使用保留名: {name}")
    if name.lower() == VIDEO_DIRNAME:
        raise HTTPException(400, "输出目录不能是视频目录: video")
    if source.prefix and name.lower() == source.prefix.lower():
        raise HTTPException(400, f"输出目录不能是源目录: {name}")
    out_dir = ds.part_dir(name)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _read_crop_meta(ds: Dataset, stem: str) -> CropImageState:
    """读取单张源图的裁剪输出记录 ``.crops/{stem}.json``.

    Args:
        ds (Dataset): 当前数据集, 用于定位记录目录.
        stem (str): 源图文件名 stem.

    Returns:
        CropImageState: 该图的部位输出记录; 无记录文件时返回空状态.

    Raises:
        pydantic.ValidationError: 记录文件内容与结构不符时向上传播.

    Callers:
        - ``routes/crop.py:get_crop_state``
        - ``routes/crop.py:_record_saved``
    """
    meta_path = ds.crop_meta_dir / f"{stem}.json"
    if not meta_path.is_file():
        return CropImageState(stem=stem)
    return CropImageState.model_validate_json(meta_path.read_text(encoding="utf-8"))


def _record_saved(ds: Dataset, stem: str, saved: list[CropSavedItem]) -> None:
    """把本轮保存的输出记录写入 ``.crops/{stem}.json`` (读-改-写, 同名部位覆盖).

    记录先写临时文件再原子替换, 不会留下写了一半的记录文件.

    Args:
        ds (Dataset): 当前数据集, 用于定位记录目录.
        stem (str): 源图文件名 stem.
        saved (list[CropSavedItem]): 本轮保存的输出列表, 输出目录名作部位键.

    Callers:
        - ``routes/crop.py:save_crops``
    """
    meta = _read_crop_meta(ds, stem)
    for item in saved:
        meta.parts[Path(item.file).parent.name] = CropPartRecord(crop=item.crop, size=item.size)
    ds.crop_meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = ds.crop_meta_dir / f"{stem}.json"
    atomic_write_text(meta_path, meta.model_dump_json(indent=2) + "\n")


def _resolve_out_path(ds: Dataset, stem: str, part_name: str, source: WorkDir) -> Path:
    """计算单张裁剪的输出路径.

    有合法 ``part_name`` 时写入 ``root/{part}/{stem}.png``;
    否则写入 ``root/crops/{stem}.png``. 同轮保存的输出路径冲突
    (部位名重复或多个未命名选区)由调用方在写盘前统一拒绝.

    Args:
        ds (Dataset): 当前数据集, 用于定位部位目录与 crops 目录.
        stem (str): 源图文件名 stem, 用作输出文件名.
        part_name (str): 候选部位名, 非空时归入对应部位目录.
        source (WorkDir): 源图所在工作目录, 透传给输出目录校验.

    Returns:
        Path: 输出 PNG 文件绝对路径.

    Raises:
        HTTPException: 部位名非法/占用保留名/与源目录相同, 或源目录为 crops
            且选区未命名(未命名输出无处可写)时抛出.

    Callers:
        - ``routes/crop.py:save_crops``
    """
    if part_name:
        out_dir = _out_dir_for(ds, part_name, source)
        return out_dir / f"{stem}.png"

    if source.prefix and source.prefix.lower() == ds.crops_dir.name:
        raise HTTPException(400, "源目录为 crops 时未命名选区无处可写, 请填写部位名")
    return ds.crops_dir / f"{stem}.png"


@router.get("/images")
def list_crop_images(ds: DatasetDep, dir: str = Query("main")) -> list[ImageThumbItem]:
    """列出指定工作目录下的源图清单(自然序).

    Args:
        ds (Dataset): 当前数据集, 由依赖注入提供.
        dir (str): 源工作目录名(数据集根下一级子目录, 默认 main).

    Returns:
        list[ImageThumbItem]: 图片清单, ``path`` 为数据集根相对路径
            (如 ``main/foo.png``), 含 stem/尺寸/缩略图哈希.

    Raises:
        HTTPException: 目录名非法或不存在时抛出 400.

    Callers:
        - ``static/js/crop.js:loadImages``
    """
    try:
        work = ds.work_dir(dir)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    result: list[ImageThumbItem] = []
    for img in work.images():
        w, h = image_size(img.abs)
        result.append(
            ImageThumbItem(
                path=img.rel,
                stem=img.stem,
                thumbnail_hash=ds.thumbnailer.hash_for(img.rel),
                width=w,
                height=h,
            )
        )
    return result


@router.get("/state")
def get_crop_state(ds: DatasetDep, path: str = Query(...)) -> CropImageState:
    """查询单张源图各部位的输出状态.

    存在性以磁盘为准: 遍历部位目录与 crops 检查 ``{stem}.png``(排除当前
    源目录自身, 源目录不兼作输出目标); 坐标与输出尺寸取自
    ``.crops/{stem}.json`` 保存记录, 未记录的输出两者为 null.

    Args:
        ds (Dataset): 当前数据集, 由依赖注入提供.
        path (str): 源图数据集根相对路径(如 ``main/foo.png``).

    Returns:
        CropImageState: 仅含磁盘实际存在输出的部位状态映射.

    Raises:
        HTTPException: 路径越界(400).

    Callers:
        - ``static/js/crop.js:loadIndex``
    """
    resolve_path(ds, path)
    stem = Path(path).stem
    source_dir = ds.work_of(path).prefix
    meta = _read_crop_meta(ds, stem)
    parts: dict[str, CropPartRecord] = {}
    for d in [*ds.list_part_dirs(), ds.crops_dir]:
        if source_dir and d.name.lower() == source_dir.lower():
            continue
        if not (d / f"{stem}.png").is_file():
            continue
        parts[d.name] = meta.parts.get(d.name) or CropPartRecord()
    return CropImageState(stem=stem, parts=parts)


@router.get("/image")
def serve_original_image(ds: DatasetDep, path: str = Query(...)) -> Response:
    """提供原图像素尺寸的 AVIF 预览(仅供框选; 保存时仍读磁盘原图).

    Args:
        ds (Dataset): 当前数据集, 由依赖注入提供.
        path (str): 数据集根相对路径(如 ``main/foo.png`` / ``gpt/foo.png``).

    Returns:
        Response: ``image/avif`` 响应体, 像素与磁盘原图一致.

    Raises:
        HTTPException: 图片不存在时返回 404.

    Callers:
        - ``static/js/crop.js:loadIndex``
    """
    src = resolve_path(ds, path)
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
def get_config(ds: DatasetDep) -> CropPageConfig:
    """裁剪页配置, 含已存在的部位目录(默认裁切由前端 localStorage 管理).

    源工作目录不经本端点提供, 由导航栏统一目录选择器经 ``/dirs`` 拉取.

    Args:
        ds (Dataset): 当前数据集, 由依赖注入提供.

    Returns:
        CropPageConfig: 含已存在的部位目录名列表.

    Callers:
        - ``static/js/crop.js:init``
    """
    return CropPageConfig(parts=[p.name for p in ds.list_part_dirs()])


@router.post("/save")
def save_crops(ds: DatasetDep, body: SaveCropsBody) -> CropSaveResponse:
    """按框裁切并写入部位目录或 ``crops/``.

    ``body.path`` 为数据集根相对路径, 源图可来自任意工作目录;
    源目录自身与保留名(main/mask/crops 等)禁止作为输出目录.
    当 ``body.no_crop_resize`` 为真时整图直接保存到部位目录;
    否则逐个选区裁切, 缩放到目标尺寸, 写入 ``root/{部位}/{stem}.png``
    或 ``root/crops/{stem}.png``; 同名输出即覆盖旧文件(重新裁剪语义).
    部位名重复或多个未命名选区导致输出路径冲突时, 在写盘前整体拒绝.

    Args:
        ds (Dataset): 当前数据集, 由依赖注入提供.
        body (SaveCropsBody): 保存参数, 含源图根相对路径/是否整图输出/部位名/选区列表.

    Returns:
        CropSaveResponse: 保存结果, 含每个输出文件的相对路径/裁剪框/尺寸/部位名.

    Raises:
        HTTPException: 图片不存在(404); 无裁剪区域(400);
            部位名非法/占用保留名/与源目录相同(400);
            部位名重复或多个未命名选区导致输出路径冲突(400).

    Callers:
        - ``static/js/crop.js:saveAndNext``
    """
    rel = body.path
    src = resolve_path(ds, rel)
    if not src.is_file():
        raise HTTPException(404, f"Image not found: {rel}")
    source = ds.work_of(rel)

    if not body.no_crop_resize and not body.crops:
        raise HTTPException(400, "没有裁剪区域")

    img = Image.open(src).convert("RGBA")
    iw, ih = img.size
    stem = Path(rel).stem
    saved: list[CropSavedItem] = []
    staged: list[tuple[Path, Path]] = []  # (临时文件, 最终路径); 全部写完后统一原子替换

    if body.no_crop_resize:
        part = (body.part_name or (body.crops[0].name if body.crops else "") or "").strip()
        out_dir = _out_dir_for(ds, part, source)
        out_path = out_dir / f"{stem}.png"
        tmp_path = out_path.with_name(out_path.name + ".tmp")
        img.save(tmp_path, format="PNG")
        staged.append((tmp_path, out_path))
        saved.append(
            CropSavedItem(
                file=str(out_path.relative_to(ds.root)),
                size=[iw, ih],
                name=part,
            )
        )
    else:
        seen_names: set[str] = set()
        for c in body.crops:
            raw = (c.name or "").strip()
            if not raw:
                continue
            if not _SAFE_NAME.match(raw):
                raise HTTPException(400, f"部位名非法: {raw}")
            if raw in seen_names:
                raise HTTPException(400, f"部位名重复: {raw}, 请为每个选区指定不同部位名")
            seen_names.add(raw)
        if len(body.crops) - len(seen_names) > 1:
            raise HTTPException(400, "多个未命名选区输出文件名冲突, 请填写部位名")

        for c in body.crops:
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
            out_path = _resolve_out_path(ds, stem, raw_name, source)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_name(out_path.name + ".tmp")
            out_img.save(tmp_path, format="PNG")
            staged.append((tmp_path, out_path))
            saved.append(
                CropSavedItem(
                    file=str(out_path.relative_to(ds.root)),
                    crop=[x1, y1, x2, y2],
                    size=[tw, th],
                    name=raw_name or out_path.parent.name,
                )
            )

    for tmp_path, out_path in staged:
        tmp_path.replace(out_path)
    _record_saved(ds, stem, saved)
    logger.info("数据集 %s 保存裁剪选区: 源图 %s, 共 %d 个输出",
                ds.name, rel, len(saved))
    return CropSaveResponse(saved=saved, count=len(saved))
