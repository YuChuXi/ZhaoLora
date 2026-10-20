"""
图片 API —— 列表 / 原图 / 色调相似. 缩略图通过 /thumb/ 静态目录直接提供. 
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from routes.deps import DatasetDep, parse_dir_names, resolve_path
from services.dataset.runtime import Dataset
from services.dataset.workdir import WorkDir
from services.online_sources.framework.base import SOURCES, file_stem as source_file_stem
from services.analysis.similarity import ColorIndex, fingerprint_file
from services.models import (
    DirsResponse,
    FilterImageItem,
    SimilarItem,
    SimilarResponse,
)
from services.analysis.scoring import load_score_json
from services.core.thumbnailer import image_size

router = APIRouter(tags=["images"])


def _color_index(ds: Dataset) -> ColorIndex:
    """取数据集的颜色索引, 首次调用时创建.

    Args:
        ds (Dataset): 当前数据集.

    Returns:
        ColorIndex: 数据集级颜色索引.

    Callers:
        - ``routes/images.py:similar_images``
    """
    if ds.color_index is None:
        ds.color_index = ColorIndex()
    return ds.color_index


def _resolve_image(ds: Dataset, rel: str) -> Path:
    """在数据集根或回收站中解析图片路径.

    Args:
        ds (Dataset): 当前数据集.
        rel (str): 数据集根相对路径.

    Returns:
        Path: 图片绝对路径.

    Raises:
        HTTPException: 两处均不存在时返回 404.

    Callers:
        - ``routes/images.py:get_image``
        - ``routes/images.py:similar_images``
    """
    main = resolve_path(ds.root, rel)
    if main.is_file():
        return main
    trash = resolve_path(ds.trash_dir, rel)
    if trash.is_file():
        return trash
    raise HTTPException(404, "文件不存在")


def _posted_at_index(work: WorkDir) -> dict[str, str]:
    """收集工作目录下各在线来源 meta 目录的发布时间索引.

    meta JSON 按帖子 pid 命名 (``.{来源}_meta/{pid}.json``), 经来源注册的
    ``file_stem`` 映射换算为下载文件 stem 后建立索引.

    Args:
        work (WorkDir): 工作目录.

    Returns:
        dict[str, str]: 下载文件 stem -> ISO8601 发布时间; 无发布时间的帖子不在索引中,
            个别损坏文件按缺失处理, 不中断列表.

    Callers:
        - ``routes/images.py:list_images``
    """
    index: dict[str, str] = {}
    for name, src in SOURCES.items():
        meta_dir = work.path / src.meta_subdir
        if not meta_dir.is_dir():
            continue
        for meta_file in meta_dir.glob("*.json"):
            try:
                posted_at = json.loads(meta_file.read_text(encoding="utf-8")).get("posted_at", "")
            except (OSError, json.JSONDecodeError):
                # meta 损坏视为缺失, 列表接口继续而非中断
                continue
            if posted_at:
                index[source_file_stem(name, meta_file.stem)] = posted_at
    return index


@router.get("/dirs")
def list_dirs(ds: DatasetDep) -> DirsResponse:
    """列出工作子目录. 引用: 筛选页 DirPicker. """
    return DirsResponse(dirs=[w.to_info() for w in ds.work_dirs()], default="main")


@router.get("/images")
def list_images(
    ds: DatasetDep,
    show_trash: bool = Query(False),
    dirs: str = Query(""),
    dir: str = Query("main"),
) -> list[FilterImageItem]:
    """列出指定子目录图片(dirs=main,head 或 dir=main). 路径相对数据集根. """
    works = ds.select_or_main(parse_dir_names(dirs, dir))

    state = ds.state_manager.get()
    selected = set(state.selected.keys())
    trashed_state = set(state.trashed.keys())
    scores = load_score_json(ds.root, ".aesthetic_scores.json")
    quality_scores = load_score_json(ds.root, ".quality_scores.json")
    result: list[FilterImageItem] = []
    seen: set[str] = set()

    for work in works:
        sub = work.name
        prefix = sub + "/"
        posted = _posted_at_index(work)
        for img in work.images():
            rel = img.rel
            if rel in trashed_state or rel in seen:
                continue
            seen.add(rel)
            w, h = image_size(img.abs)
            result.append(
                FilterImageItem(
                    path=rel,
                    stem=img.stem,
                    suffix=img.suffix,
                    size=img.size,
                    siblings=img.sibling_rels,
                    width=w,
                    height=h,
                    selected=rel in selected,
                    trashed=False,
                    thumbnail_hash=ds.thumbnailer.hash_for(rel),
                    dir=sub,
                    aesthetic=scores.get(rel),
                    quality=quality_scores.get(rel),
                    posted_at=posted.get(img.stem, ""),
                )
            )

        if not show_trash:
            continue

        trash_work = work.trashed()
        if trash_work.path.is_dir():
            for img in trash_work.images():
                if img.rel in seen:
                    continue
                seen.add(img.rel)
                w, h = image_size(img.abs)
                result.append(
                    FilterImageItem(
                        path=img.rel,
                        stem=img.stem,
                        suffix=img.suffix,
                        size=img.size,
                        siblings=img.sibling_rels,
                        width=w,
                        height=h,
                        selected=img.rel in selected,
                        trashed=True,
                        thumbnail_hash=ds.thumbnailer.hash_for(img.rel),
                        dir=sub,
                        aesthetic=scores.get(img.rel),
                        quality=quality_scores.get(img.rel),
                        posted_at=posted.get(img.stem, ""),
                    )
                )
        else:
            for rel in trashed_state:
                if not rel.startswith(prefix) or rel in seen:
                    continue
                tp = ds.trash_dir / rel
                if not tp.is_file():
                    continue
                seen.add(rel)
                w, h = image_size(tp)
                result.append(
                    FilterImageItem(
                        path=rel,
                        stem=Path(rel).stem,
                        suffix=Path(rel).suffix.lower(),
                        size=tp.stat().st_size,
                        siblings=[],
                        width=w,
                        height=h,
                        selected=rel in selected,
                        trashed=True,
                        thumbnail_hash=ds.thumbnailer.hash_for(rel),
                        dir=sub,
                        aesthetic=scores.get(rel),
                        quality=quality_scores.get(rel),
                        posted_at=posted.get(Path(rel).stem, ""),
                    )
                )

    return result


@router.get("/image")
def get_image(ds: DatasetDep, path: str = Query(...)) -> FileResponse:
    """返回原图文件(预览用). """
    file_path = _resolve_image(ds, path)
    return FileResponse(file_path)


@router.get("/images/similar")
async def similar_images(
    ds: DatasetDep,
    path: str = Query(...),
    limit: int = Query(8, ge=1, le=24),
) -> SimilarResponse:
    """按色调相似度返回若干张图(不含自身, 候选为同目录未删图). """
    rel = path.replace("\\", "/").lstrip("/")
    query_file = _resolve_image(ds.root, ds.trash_dir, rel)

    parts = rel.split("/")
    try:
        work = ds.work_dir(parts[0] if len(parts) > 1 else "main")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    state = ds.state_manager.get()
    selected = set(state.selected.keys())
    trashed_state = set(state.trashed.keys())
    candidates = [img.rel for img in work.images() if img.rel not in trashed_state]

    idx = _color_index(ds)
    idx.set_cache_dir(ds.thumbnailer.thumb_dir)

    thumb_map = {c: ds.thumbnailer.get_path(c) for c in candidates}
    idx.ensure_many(ds.root, candidates, thumb_map)

    q_thumb = ds.thumbnailer.get_path(rel) if (ds.root / rel).is_file() else None
    if q_thumb is not None and q_thumb.is_file():
        q = fingerprint_file(q_thumb)
    else:
        q = fingerprint_file(query_file)
    if q is None:
        return SimilarResponse(path=rel, similar=[])

    main_q = ds.root / rel
    if main_q.is_file():
        idx.put(rel, main_q.stat().st_mtime, q)

    hits = idx.similar_matrix(q, candidates, exclude=rel, limit=limit)

    out: list[SimilarItem] = []
    for sim_rel, dist in hits:
        await ds.thumbnailer.ensure_async(sim_rel)
        w, h = image_size(ds.root / sim_rel)
        out.append(
            SimilarItem(
                path=sim_rel,
                stem=Path(sim_rel).stem,
                width=w,
                height=h,
                selected=sim_rel in selected,
                trashed=sim_rel in trashed_state,
                thumbnail_hash=ds.thumbnailer.hash_for(sim_rel),
                distance=round(dist, 6),
            )
        )
    return SimilarResponse(path=rel, similar=out)
