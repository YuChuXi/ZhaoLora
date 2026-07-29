"""Tag 频率统计与清洗 API（数据集作用域）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs
from services.scoring import load_score_json
from services.jobs import EmitFn
from services.models import JobSnapshot, JobSubmitResponse, TagCleanResult, TagStatsResult, TxtDirInfo
from services.tag_clean import batch_edit_tags, clean_txt_dirs
from services.tag_stats import count_tags, list_txt_subdirs, load_translation_map, resolve_selected_dirs, translate_missing_tags

router = APIRouter(prefix="/tags", tags=["tags"])


class TagDirsResponse(BaseModel):
    """含 txt 的子目录列表。引用：``GET /tags/dirs``。"""

    root: str
    dirs: list[TxtDirInfo]


def _root(request: Request) -> Path:
    """引用：本模块各接口。"""
    return get_runtime(request).root


class StatsBody(BaseModel):
    """标签频率统计请求。引用：``POST /tags/stats``。"""

    dirs: list[str] | None = Field(default=None, description="子目录名，空/省略=全选")
    lower: bool = True
    underscores: bool = True
    html_unescape: bool = True
    per_sample: bool = True
    min_count: int = 1


class CleanBody(BaseModel):
    """标签清洗请求。引用：``POST /tags/clean``。"""

    dirs: list[str] | None = Field(default=None, description="子目录名")
    drop: list[str] = Field(default_factory=list, description="整词精确删除")
    drop_contains: list[str] = Field(default_factory=list, description="子串删除")
    promote: list[str] = Field(default_factory=list, description="将已存在标签提升至头部")
    prepend: list[str] = Field(default_factory=list, description="头部前置（可新增）")
    promote_separator: bool = Field(False, description="以 ||| 分隔头部与正文")
    inject_pony_score: bool = Field(
        False, description="根据 .aesthetic_scores.json 写入 score_0…score_9（位于头部前置之前）"
    )
    inject_quality_tag: bool = Field(
        False, description="根据 .quality_scores.json 写入质量标签（masterpiece/worst quality 等）"
    )
    dry_run: bool = True
    sample_limit: int = Field(default=40, ge=0, le=200)


@router.get("/dirs")
def tag_dirs(request: Request) -> TagDirsResponse:
    """列出含旁路 txt 的目录。引用：标签/清洗页。"""
    root = _root(request)
    return TagDirsResponse(root=str(root), dirs=list_txt_subdirs(root))


@router.post("/stats")
def tag_stats(request: Request, body: StatsBody) -> TagStatsResult:
    """统计标签频率。引用：标签页。"""
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
    return result.model_copy(
        update={
            "dirs": [p.name if p != root else "." for p in selected],
            "root": str(root),
            "zh_cache_size": len(zh_map),
        }
    )


class TranslateMissingBody(BaseModel):
    """批量翻译缺失标签请求。引用：``POST /tags/translate-missing``。"""

    tags: list[str] = Field(..., min_length=1)


@router.post("/translate-missing")
async def translate_missing(request: Request, body: TranslateMissingBody) -> dict:
    """
    异步批量翻译标签，保存到共享缓存。

    引用：tags.js。
    """
    root = _root(request)
    result = await translate_missing_tags(body.tags, root)
    return {"translated": len(result), "map": result}


@router.post("/clean")
async def tag_clean(request: Request, body: CleanBody) -> JobSubmitResponse:
    """
    提交标签清洗长任务。

    返回 job_id；结束事件的 result 为清洗结果。
    引用：tag_clean.js。
    """
    rt = get_runtime(request)
    root = rt.root
    selected = resolve_selected_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    scores = load_score_json(root, ".aesthetic_scores.json") if body.inject_pony_score else {}
    quality_scores = load_score_json(root, ".quality_scores.json") if body.inject_quality_tag else {}
    payload = body

    def run(emit: EmitFn) -> TagCleanResult:
        """工作线程：清洗旁路 txt。引用：``JobManager.submit``。"""
        emit(
            JobSnapshot(
                status="running",
                phase="cleaning",
                message="预览中…" if payload.dry_run else "写入中…",
                pct=0,
            )
        )
        result = clean_txt_dirs(
            selected,
            drop=payload.drop,
            drop_contains=payload.drop_contains,
            promote=payload.promote,
            prepend=payload.prepend,
            promote_separator=payload.promote_separator,
            inject_pony_score=payload.inject_pony_score,
            dataset_root=root,
            aesthetic_scores=scores,
            inject_quality_tag=payload.inject_quality_tag,
            quality_scores=quality_scores,
            dry_run=payload.dry_run,
            sample_limit=payload.sample_limit,
        )
        result = result.model_copy(
            update={
                "dirs": [p.name if p != root else "." for p in selected],
                "root": str(root),
            }
        )
        emit(
            JobSnapshot(
                status="done",
                phase="done",
                message="完成",
                pct=100,
                result=result,
            )
        )
        return result

    job_id = jobs(request).submit(kind="tag_clean", group="compute", dataset=rt.name, run=run)
    return JobSubmitResponse(ok=True, job_id=job_id)


class BatchEditBody(BaseModel):
    """批量编辑标签请求。引用：``POST /tags/batch-edit``。"""

    dirs: list[str] | None = Field(default=None, description="子目录名")
    delete_tags: list[str] = Field(default_factory=list, description="要删除的标签")
    add_tags: list[str] = Field(default_factory=list, description="要添加的标签")
    position: str = Field("after", description="放在 ||| 前(before) 或 后(after)")
    insert_mode: str = Field("bottom", description="置顶(top) / 置底(bottom) / 随机(random)")
    conditional: bool = Field(False, description="仅当已有同名标签时才插入")
    keep_duplicates: bool = Field(False, description="不删除已有重复标签")
    inject_pony_score: bool = Field(False, description="注入 Pony 美学档位，永远置于最前")
    dry_run: bool = True
    sample_limit: int = Field(default=40, ge=0, le=200)


@router.post("/batch-edit")
async def tag_batch_edit(request: Request, body: BatchEditBody) -> JobSubmitResponse:
    """
    批量删除/添加标签。

    返回 job_id；结束事件的 result 为清洗结果。
    引用：tag_clean.js。
    """
    rt = get_runtime(request)
    root = rt.root
    selected = resolve_selected_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")
    if not body.delete_tags and not body.add_tags:
        raise HTTPException(400, "请指定要删除或添加的标签")

    if body.position not in ("before", "after"):
        raise HTTPException(400, "position 须为 before 或 after")
    if body.insert_mode not in ("top", "bottom", "random"):
        raise HTTPException(400, "insert_mode 须为 top / bottom / random")

    scores = load_score_json(root, ".aesthetic_scores.json") if body.inject_pony_score else {}
    quality_scores = load_score_json(root, ".quality_scores.json") if body.inject_quality_tag else {}
    payload = body

    def run(emit: EmitFn) -> TagCleanResult:
        """工作线程：批量编辑旁路 txt。引用：``JobManager.submit``。"""
        emit(
            JobSnapshot(
                status="running",
                phase="editing",
                message="预览中…" if payload.dry_run else "写入中…",
                pct=0,
            )
        )
        result = batch_edit_tags(
            selected,
            delete_tags=payload.delete_tags,
            add_tags=payload.add_tags,
            position=payload.position,
            insert_mode=payload.insert_mode,
            conditional=payload.conditional,
            keep_duplicates=payload.keep_duplicates,
            inject_pony_score=payload.inject_pony_score,
            dataset_root=root,
            aesthetic_scores=scores,
            inject_quality_tag=payload.inject_quality_tag,
            quality_scores=quality_scores,
            dry_run=payload.dry_run,
            sample_limit=payload.sample_limit,
        )
        result = result.model_copy(
            update={
                "dirs": [p.name if p != root else "." for p in selected],
                "root": str(root),
            }
        )
        emit(
            JobSnapshot(
                status="done",
                phase="done",
                message="完成",
                pct=100,
                result=result,
            )
        )
        return result

    job_id = jobs(request).submit(kind="tag_batch_edit", group="compute", dataset=rt.name, run=run)
    return JobSubmitResponse(ok=True, job_id=job_id)