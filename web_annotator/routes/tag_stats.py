"""Tag 频率统计 API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.dataset_layout import DatasetLayout
from services.tag_stats import count_tags, list_txt_subdirs, load_translation_map, resolve_selected_dirs

router = APIRouter(prefix="/api/tags", tags=["tags"])


def _layout(request: Request) -> DatasetLayout:
    return request.app.state.layout


def _root(request: Request) -> Path:
    return _layout(request).root


class StatsBody(BaseModel):
    dirs: list[str] | None = Field(default=None, description="子目录名，空/省略=全选")
    lower: bool = True
    underscores: bool = True
    html_unescape: bool = True
    per_sample: bool = True
    min_count: int = 1


@router.get("/dirs")
def tag_dirs(request: Request):
    root = _root(request)
    dirs = list_txt_subdirs(root)
    return {
        "root": str(root),
        "dirs": dirs,
    }


@router.post("/stats")
def tag_stats(request: Request, body: StatsBody):
    root = _root(request)
    selected = resolve_selected_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")
    if body.min_count < 1:
        raise HTTPException(400, "最少出现次数至少为 1")

    zh_map = load_translation_map(root)
    result = count_tags(
        selected,
        zh_map=zh_map,
        lower=body.lower,
        underscores=body.underscores,
        html_unescape=body.html_unescape,
        per_sample=body.per_sample,
        min_count=body.min_count,
    )
    result["dirs"] = [p.name if p != root else "." for p in selected]
    result["root"] = str(root)
    result["zh_cache_size"] = len(zh_map)
    return result
