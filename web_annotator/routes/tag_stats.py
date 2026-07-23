"""Tag 频率统计与清洗 API。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.aesthetic import load_scores
from services.dataset_layout import DatasetLayout
from services.tag_clean import clean_txt_dirs
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


class CleanBody(BaseModel):
    dirs: list[str] | None = Field(default=None, description="子目录名")
    drop: list[str] = Field(default_factory=list, description="整词精确删除")
    drop_contains: list[str] = Field(default_factory=list, description="子串删除")
    promote: list[str] = Field(default_factory=list, description="将已存在标签提升至头部")
    prepend: list[str] = Field(default_factory=list, description="头部前置（可新增）")
    promote_separator: bool = Field(False, description="以 ||| 分隔头部与正文")
    inject_pony_score: bool = Field(
        False, description="根据 .aesthetic_scores.json 写入 score_0…score_9（位于头部前置之前）"
    )
    dry_run: bool = True
    sample_limit: int = Field(default=40, ge=0, le=200)


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


@router.post("/clean")
def tag_clean(request: Request, body: CleanBody):
    """按 clean_dataset_tags 规则清洗所选目录的 .txt。"""
    root = _root(request)
    selected = resolve_selected_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    scores = load_scores(root) if body.inject_pony_score else {}
    result = clean_txt_dirs(
        selected,
        drop=body.drop,
        drop_contains=body.drop_contains,
        promote=body.promote,
        prepend=body.prepend,
        promote_separator=body.promote_separator,
        inject_pony_score=body.inject_pony_score,
        dataset_root=root,
        aesthetic_scores=scores,
        dry_run=body.dry_run,
        sample_limit=body.sample_limit,
    )
    result["dirs"] = [p.name if p != root else "." for p in selected]
    result["root"] = str(root)
    return result
