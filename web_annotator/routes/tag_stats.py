"""Tag 频率统计与清洗 API(数据集作用域). """

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs, resolve_path
from services.analysis.scoring import load_score_json
from services.core.jobs import EmitFn
from services.core.scanner import scan_images
from services.core.thumbnailer import image_size
from services.core.work_dir import list_work_dirs
from services.models import (
    JobSnapshot,
    JobSubmitResponse,
    TAG_CHECK_POSITIONS,
    TagCheckBody,
    TagCheckItem,
    TagCheckResponse,
    TagCleanResult,
    TagEditOneBody,
    TagStatsResult,
    WorkDirInfo,
)
from services.tagging.clean import add_tag_to_caption, batch_edit_tags, check_tag_in_caption, clean_txt_dirs, delete_tag_from_caption
from services.tagging.stats import count_tags, load_translation_map, resolve_selected_dirs, translate_missing_tags

router = APIRouter(prefix="/tags", tags=["tags"])


class TagDirsResponse(BaseModel):
    """工作子目录列表. 引用: ``GET /tags/dirs``. """

    root: str
    dirs: list[WorkDirInfo]


def _root(request: Request) -> Path:
    """引用: 本模块各接口. """
    return get_runtime(request).root


class StatsBody(BaseModel):
    """标签频率统计请求. 引用: ``POST /tags/stats``. """

    dirs: list[str] | None = Field(default=None, description="子目录名, 空/省略=全选")
    lower: bool = True
    underscores: bool = True
    html_unescape: bool = True
    per_sample: bool = True
    min_count: int = 1


class CleanBody(BaseModel):
    """标签清洗请求. 引用: ``POST /tags/clean``. """

    dirs: list[str] | None = Field(default=None, description="子目录名")
    drop: list[str] = Field(default_factory=list, description="整词精确删除")
    drop_contains: list[str] = Field(default_factory=list, description="子串删除")
    promote: list[str] = Field(default_factory=list, description="将已存在标签提升至头部")
    prepend: list[str] = Field(default_factory=list, description="头部前置(可新增)")
    promote_separator: bool = Field(False, description="以 ||| 分隔头部与正文")
    inject_pony_score: bool = Field(
        False, description="根据 .aesthetic_scores.json 写入 score_0…score_9(位于头部前置之前)"
    )
    inject_quality_tag: bool = Field(
        False, description="根据 .quality_scores.json 写入质量标签(masterpiece/worst quality 等)"
    )
    dry_run: bool = True
    sample_limit: int = Field(default=40, ge=0, le=200)


@router.get("/dirs")
def tag_dirs(request: Request) -> TagDirsResponse:
    """列出工作子目录(含图片的一级子目录). 引用: 标签/标签检查页. """
    root = _root(request)
    return TagDirsResponse(root=str(root), dirs=list_work_dirs(root))


@router.post("/stats")
def tag_stats(request: Request, body: StatsBody) -> TagStatsResult:
    """统计标签频率. 引用: 标签页. """
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
    """批量翻译缺失标签请求. 引用: ``POST /tags/translate-missing``. """

    tags: list[str] = Field(..., min_length=1)


@router.post("/translate-missing")
async def translate_missing(request: Request, body: TranslateMissingBody) -> dict:
    """
    异步批量翻译标签, 保存到共享缓存. 

    引用: tags.js. 
    """
    root = _root(request)
    result = await translate_missing_tags(body.tags, root)
    return {"translated": len(result), "map": result}


@router.post("/clean")
async def tag_clean(request: Request, body: CleanBody) -> JobSubmitResponse:
    """
    提交标签清洗长任务.

    返回 job_id；结束事件的 result 为清洗结果.
    引用: ``POST /tags/clean`` API.
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
        """工作线程: 清洗旁路 txt. 引用: ``JobManager.submit``. """
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
    """批量编辑标签请求. 引用: ``POST /tags/batch-edit``. """

    dirs: list[str] | None = Field(default=None, description="子目录名")
    delete_tags: list[str] = Field(default_factory=list, description="要删除的标签")
    add_tags: list[str] = Field(default_factory=list, description="要添加的标签")
    replace_pairs: list[list[str]] = Field(default_factory=list, description="原位置替换 [[原标签, 新标签], ...]")
    position: str = Field("before", description="放在 ||| 前(before) 或 后(after)")
    insert_mode: str = Field("top", description="置顶(top) / 置底(bottom) / 随机(random)")
    conditional: bool = Field(False, description="仅当已有同名标签时才插入")
    keep_duplicates: bool = Field(False, description="不删除已有重复标签")
    inject_pony_score: bool = Field(False, description="注入 Pony 美学档位, 永远置于最前")
    inject_quality_tag: bool = Field(False, description="注入质量标签, 永远置于最前")
    separator_mode: str = Field("keep", description="||| 分隔模式: keep(保留) | add(添加) | remove(删除)")
    dry_run: bool = True
    sample_limit: int = Field(default=40, ge=0, le=200)


@router.post("/batch-edit")
async def tag_batch_edit(request: Request, body: BatchEditBody) -> JobSubmitResponse:
    """
    批量删除/添加标签.

    返回 job_id；结束事件的 result 为清洗结果.
    引用: ``tags.js``.
    """
    rt = get_runtime(request)
    root = rt.root
    selected = resolve_selected_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")
    if not body.delete_tags and not body.add_tags and not body.replace_pairs:
        raise HTTPException(400, "请指定要删除、添加或替换的标签")

    if body.position not in ("before", "after"):
        raise HTTPException(400, "position 须为 before 或 after")
    if body.insert_mode not in ("top", "bottom", "random"):
        raise HTTPException(400, "insert_mode 须为 top / bottom / random")

    scores = load_score_json(root, ".aesthetic_scores.json") if body.inject_pony_score else {}
    quality_scores = load_score_json(root, ".quality_scores.json") if body.inject_quality_tag else {}
    payload = body

    def run(emit: EmitFn) -> TagCleanResult:
        """工作线程: 批量编辑旁路 txt. 引用: ``JobManager.submit``. """
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
            replace_pairs=[(p[0], p[1]) for p in payload.replace_pairs if len(p) >= 2],
            position=payload.position,
            insert_mode=payload.insert_mode,
            conditional=payload.conditional,
            keep_duplicates=payload.keep_duplicates,
            inject_pony_score=payload.inject_pony_score,
            dataset_root=root,
            aesthetic_scores=scores,
            inject_quality_tag=payload.inject_quality_tag,
            quality_scores=quality_scores,
            use_separator=payload.separator_mode,
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


def _build_check_item(
    img_path_rel: str,
    stem: str,
    work_dir: Path,
    dir_name: str,
    root: Path,
    thumbnailer,
    tag: str,
    position: str,
) -> TagCheckItem:
    """为单张图片构建标签检查结果.

    读取与图片同 stem 的旁路 ``.txt``, 调用 ``check_tag_in_caption`` 判断
    标签存在性与位置, 并计算缩略图 hash 与图片宽高.

    Args:
        img_path_rel: 图片相对数据集根的路径(如 ``main/foo.png``).
        stem: 图片 stem.
        work_dir: 图片所在的绝对路径目录.
        dir_name: 目录名(用于前端展示).
        root: 数据集根目录.
        thumbnailer: 缩略图管理器.
        tag: 待检查的标签.
        position: 位置选项.

    Returns:
        TagCheckItem: 检查结果项.

    Callers:
        - ``tag_check``
        - ``tag_edit_one``
    """
    txt_path = work_dir / (stem + ".txt")
    raw = ""
    if txt_path.is_file():
        raw = txt_path.read_text(encoding="utf-8")
    exists, position_ok = check_tag_in_caption(raw, tag, position)
    abs_img = root / img_path_rel
    w, h = image_size(abs_img) if abs_img.is_file() else (0, 0)
    return TagCheckItem(
        path=img_path_rel,
        stem=stem,
        width=w,
        height=h,
        thumbnail_hash=thumbnailer.hash_for(img_path_rel),
        dir=dir_name,
        exists=exists,
        position_ok=position_ok,
        caption_preview=raw.strip().replace("\n", " ")[:200],
    )


@router.post("/check")
def tag_check(request: Request, body: TagCheckBody) -> TagCheckResponse:
    """批量检查目录内图片旁路 ``.txt`` 中标签的存在与位置.

    引用: ``tag_check.js``.
    """
    rt = get_runtime(request)
    root = rt.root
    if body.position not in TAG_CHECK_POSITIONS:
        raise HTTPException(400, f"position 非法: {body.position}")

    tag = body.tag.strip()
    check_enabled = bool(tag)
    selected = resolve_selected_dirs(root, body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")

    thumbnailer = rt.thumbnailer
    items: list[TagCheckItem] = []
    for work in selected:
        if work == root:
            dir_name, prefix = ".", ""
        else:
            dir_name, prefix = work.name, work.name
        for img in scan_images(work, path_prefix=prefix):
            if check_enabled:
                items.append(
                    _build_check_item(
                        img.path, img.stem, work, dir_name, root, thumbnailer, tag, body.position
                    )
                )
            else:
                abs_img = root / img.path
                w, h = image_size(abs_img) if abs_img.is_file() else (0, 0)
                items.append(
                    TagCheckItem(
                        path=img.path,
                        stem=img.stem,
                        width=w,
                        height=h,
                        thumbnail_hash=thumbnailer.hash_for(img.path),
                        dir=dir_name,
                        exists=False,
                        position_ok=False,
                        caption_preview="",
                    )
                )

    ok_count = sum(1 for it in items if it.position_ok)
    return TagCheckResponse(
        tag=tag,
        position=body.position,
        total=len(items),
        ok_count=ok_count,
        bad_count=len(items) - ok_count,
        items=items,
    )


@router.post("/edit-one")
def tag_edit_one(request: Request, body: TagEditOneBody) -> TagCheckItem:
    """编辑单张图片的旁路 ``.txt``, 在指定位置添加标签后返回最新检查结果.

    引用: ``tag_check.js``.
    """
    rt = get_runtime(request)
    root = rt.root
    if body.position not in TAG_CHECK_POSITIONS:
        raise HTTPException(400, f"position 非法: {body.position}")
    if not body.tag.strip():
        raise HTTPException(400, "标签不能为空")

    tag = body.tag.strip()
    img_abs = resolve_path(root, body.path)
    if not img_abs.is_file():
        raise HTTPException(404, "图片不存在")

    txt_path = img_abs.with_suffix(".txt")
    raw = ""
    if txt_path.is_file():
        raw = txt_path.read_text(encoding="utf-8")

    new_caption = add_tag_to_caption(raw, tag, body.position)
    txt_path.write_text(new_caption + "\n", encoding="utf-8")

    stem = img_abs.stem
    work_dir = img_abs.parent
    rel = body.path.replace("\\", "/").lstrip("/")
    dir_name = rel.split("/")[0] if "/" in rel else "."
    return _build_check_item(
        rel, stem, work_dir, dir_name, root, rt.thumbnailer, tag, body.position
    )


class TagDeleteOneBody(BaseModel):
    """单文件标签删除请求. 引用: ``POST /tags/delete-one``."""

    path: str = Field(..., description="图片相对数据集根的路径")
    tag: str


@router.post("/delete-one")
def tag_delete_one(request: Request, body: TagDeleteOneBody) -> TagCheckItem:
    """从单张图片旁路 ``.txt`` 中删除指定标签后返回最新检查结果.

    引用: ``tag_check.js``.
    """
    rt = get_runtime(request)
    root = rt.root
    if not body.tag.strip():
        raise HTTPException(400, "标签不能为空")

    tag = body.tag.strip()
    img_abs = resolve_path(root, body.path)
    if not img_abs.is_file():
        raise HTTPException(404, "图片不存在")

    txt_path = img_abs.with_suffix(".txt")
    raw = ""
    if txt_path.is_file():
        raw = txt_path.read_text(encoding="utf-8")

    new_caption = delete_tag_from_caption(raw, tag)
    txt_path.write_text(new_caption + "\n", encoding="utf-8")

    stem = img_abs.stem
    work_dir = img_abs.parent
    rel = body.path.replace("\\", "/").lstrip("/")
    dir_name = rel.split("/")[0] if "/" in rel else "."
    return _build_check_item(
        rel, stem, work_dir, dir_name, root, rt.thumbnailer, tag, "any"
    )