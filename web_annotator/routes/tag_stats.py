"""Tag 频率统计与清洗 API(数据集作用域). """

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routes.deps import DatasetDep, jobs, resolve_path
from services.analysis.scoring import load_score_json
from services.core.atomic import atomic_write_text
from services.core.jobs import EmitFn
from services.core.natural import natural_key
from services.core.thumbnailer import image_size
from services.dataset.workdir import WorkDir
from services.models import (
    JobSnapshot,
    JobSubmitResponse,
    TAG_CHECK_POSITIONS,
    TAG_INSERT_POSITIONS,
    TagCheckBody,
    TagCheckItem,
    TagCheckResponse,
    TagCleanResult,
    TagEditOneBody,
    TagStatsResult,
    WorkDirInfo,
    EmbeddingStatusResponse,
    EmbeddingQueryBody,
    EmbeddingQueryItem,
    EmbeddingQueryResult,
    EmbeddingHistoryItem,
    EmbeddingHistoryResponse,
    EmbeddingBatchTagBody,
    EmbeddingBatchTagResponse,
    ConceptQueryBody,
    ConceptQueryItem,
    ConceptQueryResult,
)
from services.tagging.clean import add_tag_to_caption, batch_edit_tags, check_tag_in_caption, clean_txt_dirs, delete_tag_from_caption, split_header_body, join_tags_line
from services.tagging.stats import count_tags, load_translation_map, translate_missing_tags
from services.analysis.reranker import (
    rerank,
    model_ready as reranker_model_ready,
)
from services.analysis.sam3 import (
    run_concept_query,
    model_ready as concept_model_ready,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tags", tags=["tags"])


class TagDirsResponse(BaseModel):
    """工作子目录列表."""

    dirs: list[WorkDirInfo]


class StatsBody(BaseModel):
    """标签频率统计请求."""

    dirs: list[str] | None = Field(default=None, description="子目录名, 空/省略=全选")
    lower: bool = True
    underscores: bool = True
    html_unescape: bool = True
    per_sample: bool = True
    min_count: int = 1
    section: str = Field(default="all", description="统计范围: all | before | after")
    include: list[str] = Field(default_factory=list, description="样本须命中的标签模式; 空表示不限")
    exclude: list[str] = Field(default_factory=list, description="样本须未命中的标签模式; 空表示不限")
    match_mode: str = Field("exact", description="标签匹配模式: exact | regex | danbooru")


class CleanBody(BaseModel):
    """标签清洗请求."""

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
def dirs(ds: DatasetDep) -> TagDirsResponse:
    """列出工作子目录(含图片的一级子目录)。

    Args:
        ds (DatasetDep): 当前数据集运行时依赖。

    Returns:
        TagDirsResponse: 工作子目录列表。

    Callers:
        - ``static/js/nav.js:dirsApi``
    """
    return TagDirsResponse(dirs=[w.to_info() for w in ds.work_dirs()])


@router.post("/stats")
def stats(ds: DatasetDep, body: StatsBody) -> TagStatsResult:
    """统计标签频率。

    Args:
        ds (DatasetDep): 当前数据集运行时依赖。
        body (StatsBody): 统计参数, 含目录、采样、过滤等选项。

    Returns:
        TagStatsResult: 标签频率统计结果。

    Callers:
        - ``static/js/tags.js:refresh``
        - ``static/js/tag_check.js:loadTags``
    """
    root = ds.root
    selected = ds.select(body.dirs)
    if not selected:
        raise HTTPException(400, "请选择目录")
    if body.min_count < 1:
        raise HTTPException(400, "最少出现次数至少为 1")
    if body.section not in ("all", "before", "after"):
        raise HTTPException(400, "section 须为 all / before / after")
    if body.match_mode not in ("exact", "regex", "danbooru"):
        raise HTTPException(400, "match_mode 须为 exact / regex / danbooru")

    zh_map = load_translation_map(root)
    result = count_tags(
        [w.path for w in selected],
        zh_map=zh_map,
        lower=body.lower,
        underscores=body.underscores,
        html_unescape=body.html_unescape,
        per_sample=body.per_sample,
        min_count=body.min_count,
        section=body.section,
        include=body.include or None,
        exclude=body.exclude or None,
        match_mode=body.match_mode,
    )
    logger.info(
        "标签统计 dataset=%s dirs=%s min_count=%d section=%s",
        ds.name,
        len(selected),
        body.min_count,
        body.section,
    )
    return result.model_copy(
        update={
            "dirs": [w.name for w in selected],
            "zh_cache_size": len(zh_map),
        }
    )


class TranslateMissingBody(BaseModel):
    """批量翻译缺失标签请求."""

    tags: list[str] = Field(..., min_length=1)


@router.post("/translate-missing")
async def translate_missing(ds: DatasetDep, body: TranslateMissingBody) -> dict:
    """异步批量翻译标签, 保存到共享缓存。

    Args:
        ds (DatasetDep): 当前数据集运行时依赖。
        body (TranslateMissingBody): 待翻译标签列表。

    Returns:
        dict: ``{"translated": int, "map": dict}``, 含已翻译数量与映射表。

    Callers:
        - ``static/js/tags.js:translateMissing``
    """
    root = ds.root
    result = await translate_missing_tags(body.tags, root)
    logger.info(
        "补译缺失标签 dataset=%s 目标=%d 已翻译=%d",
        request.state.runtime.root.name,
        len(body.tags),
        len(result),
    )
    return {"translated": len(result), "map": result}


@router.post("/clean")
async def clean(ds: DatasetDep, body: CleanBody) -> JobSubmitResponse:
    """提交标签清洗长任务。

    返回 job_id；结束事件的 result 为清洗结果。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (CleanBody): 清洗参数, 含删除/前置/注入等选项。

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度。
    """
    rt = ds
    root = rt.root
    works = rt.select(body.dirs)
    if not works:
        raise HTTPException(400, "请选择目录")

    scores = load_score_json(root, ".aesthetic_scores.json") if body.inject_pony_score else {}
    quality_scores = load_score_json(root, ".quality_scores.json") if body.inject_quality_tag else {}
    payload = body

    def run(emit: EmitFn) -> TagCleanResult:
        """工作线程: 清洗旁路 txt。

        Args:
            emit (EmitFn): 任务事件推送回调。

        Returns:
            TagCleanResult: 清洗结果。

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        emit(
            JobSnapshot(
                status="running",
                phase="cleaning",
                message="预览中…" if payload.dry_run else "写入中…",
                pct=0,
            )
        )
        result = clean_txt_dirs(
            [w.path for w in works],
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
                "dirs": [w.name for w in works],
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

    job_id = await jobs(request).submit(kind="tag_clean", group="compute", dataset=rt.name, run=run)
    logger.info(
        "标签清洗 dataset=%s dirs=%s 模式=%s job_id=%s",
        rt.name,
        len(selected),
        "dry-run" if payload.dry_run else "write",
        job_id,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)


class BatchEditBody(BaseModel):
    """批量编辑标签请求."""

    dirs: list[str] | None = Field(default=None, description="子目录名")
    delete_tags: list[str] = Field(default_factory=list, description="要删除的标签")
    add_tags: list[str] = Field(default_factory=list, description="要添加的标签")
    replace_pairs: list[list[str]] = Field(default_factory=list, description="原位置替换 [[原标签, 新标签], ...]")
    match_mode: str = Field("exact", description="delete/replace 原标签匹配模式: exact | regex | danbooru")
    position: str = Field("before", description="放在 ||| 前(before) 或 后(after)")
    insert_mode: str = Field("top", description="置顶(top) / 置底(bottom) / 随机(random)")
    conditional: bool = Field(False, description="仅当已有同名标签时才插入")
    keep_duplicates: bool = Field(False, description="不删除已有重复标签")
    inject_pony_score: bool = Field(False, description="注入 Pony 美学档位, 永远置于最前")
    inject_quality_tag: bool = Field(False, description="注入质量标签, 永远置于最前")
    inject_year_tag: bool = Field(False, description="按在线来源 meta JSON 发布时间注入年份标签(如 year 2025), 置于 ||| 之前头部")
    separator_mode: str = Field("keep", description="||| 分隔模式: keep(保留) | add(添加) | remove(删除)")
    dry_run: bool = True
    sample_limit: int = Field(default=40, ge=0, le=200)


@router.post("/batch-edit")
async def batch_edit(ds: DatasetDep, body: BatchEditBody) -> JobSubmitResponse:
    """批量删除/添加标签。

    返回 job_id；结束事件的 result 为清洗结果。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (BatchEditBody): 批量编辑参数, 含增删/替换/位置/分隔等选项。

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度。

    Callers:
        - ``static/js/tags.js:runBatchEdit``
    """
    rt = ds
    root = rt.root
    works = rt.select(body.dirs)
    if not works:
        raise HTTPException(400, "请选择目录")
    if (
        not body.delete_tags and not body.add_tags and not body.replace_pairs
        and not (body.inject_pony_score or body.inject_quality_tag or body.inject_year_tag)
    ):
        raise HTTPException(400, "请指定要删除、添加或替换的标签")

    if body.position not in ("before", "after"):
        raise HTTPException(400, "position 须为 before 或 after")
    if body.insert_mode not in ("top", "bottom", "random"):
        raise HTTPException(400, "insert_mode 须为 top / bottom / random")
    if body.match_mode not in ("exact", "regex", "danbooru"):
        raise HTTPException(400, "match_mode 须为 exact / regex / danbooru")

    scores = load_score_json(root, ".aesthetic_scores.json") if body.inject_pony_score else {}
    quality_scores = load_score_json(root, ".quality_scores.json") if body.inject_quality_tag else {}
    payload = body

    def run(emit: EmitFn) -> TagCleanResult:
        """工作线程: 批量编辑旁路 txt。

        Args:
            emit (EmitFn): 任务事件推送回调。

        Returns:
            TagCleanResult: 编辑结果。

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        emit(
            JobSnapshot(
                status="running",
                phase="editing",
                message="预览中…" if payload.dry_run else "写入中…",
                pct=0,
            )
        )
        result = batch_edit_tags(
            [w.path for w in works],
            delete_tags=payload.delete_tags,
            add_tags=payload.add_tags,
            replace_pairs=[(p[0], p[1]) for p in payload.replace_pairs if len(p) >= 2],
            match_mode=payload.match_mode,
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

    job_id = await jobs(request).submit(kind="tag_batch_edit", group="compute", dataset=rt.name, run=run)
    logger.info(
        "批量编辑标签 dataset=%s dirs=%s 删除=%d 新增=%d 替换=%d 模式=%s job_id=%s",
        rt.name,
        len(selected),
        len(body.delete_tags),
        len(body.add_tags),
        len(body.replace_pairs),
        "dry-run" if payload.dry_run else "write",
        job_id,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)


def _build_check_item(
    work: WorkDir,
    img_path_rel: str,
    thumbnailer,
    tag: str,
    position: str,
) -> TagCheckItem:
    """为单张图片构建标签检查结果。

    读取与图片同 stem 的旁路 ``.txt``, 调用 ``check_tag_in_caption`` 判断
    标签存在性与位置, 并计算缩略图 hash 与图片宽高。

    Args:
        work (WorkDir): 图片所在工作目录。
        img_path_rel (str): 图片相对数据集根的路径(如 ``main/foo.png``)。
        thumbnailer (Thumbnailer): 缩略图管理器。
        tag (str): 待检查的标签。
        position (str): 位置选项。

    Returns:
        TagCheckItem: 检查结果项。

    Callers:
        - ``routes/tag_stats.py:check``
        - ``routes/tag_stats.py:edit_one``
        - ``routes/tag_stats.py:delete_one``
    """
    stem = Path(img_path_rel).stem
    txt_path = work.path / (stem + ".txt")
    raw = ""
    if txt_path.is_file():
        raw = txt_path.read_text(encoding="utf-8")
    exists, position_ok = check_tag_in_caption(raw, tag, position)
    abs_img = work.path / Path(img_path_rel).name
    w, h = image_size(abs_img) if abs_img.is_file() else (0, 0)
    return TagCheckItem(
        path=img_path_rel,
        stem=stem,
        width=w,
        height=h,
        thumbnail_hash=thumbnailer.hash_for(img_path_rel),
        dir=work.name,
        exists=exists,
        position_ok=position_ok,
        caption_preview=raw.strip().replace("\n", " "),
    )


@router.post("/check")
def check(ds: DatasetDep, body: TagCheckBody) -> TagCheckResponse:
    """批量检查目录内图片旁路 ``.txt`` 中标签的存在与位置。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (TagCheckBody): 检查参数, 含标签、目录、位置等。

    Returns:
        TagCheckResponse: 检查结果, 含每张图片的存在/位置状态。

    Callers:
        - ``static/js/tag_check.js:runCheck``
    """
    rt = ds
    root = rt.root
    if body.position not in TAG_CHECK_POSITIONS:
        raise HTTPException(400, f"position 非法: {body.position}")

    tag = body.tag.strip()
    works = rt.select(body.dirs)
    if not works:
        raise HTTPException(400, "请选择目录")

    thumbnailer = rt.thumbnailer
    items: list[TagCheckItem] = []
    for work in works:
        for img in work.images():
            items.append(
                _build_check_item(work, img.rel, thumbnailer, tag, body.position)
            )

    ok_count = sum(1 for it in items if it.position_ok)
    logger.info(
        "标签检查 dataset=%s tag=%s total=%d ok=%d bad=%d",
        rt.name,
        tag,
        len(items),
        ok_count,
        len(items) - ok_count,
    )
    return TagCheckResponse(
        tag=tag,
        position=body.position,
        total=len(items),
        ok_count=ok_count,
        bad_count=len(items) - ok_count,
        items=items,
    )


@router.post("/edit-one")
def edit_one(ds: DatasetDep, body: TagEditOneBody) -> TagCheckItem:
    """编辑单张图片的旁路 ``.txt``, 在指定位置添加标签后返回最新检查结果。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (TagEditOneBody): 编辑参数, 含路径、标签、插入位置与检查位置。

    Returns:
        TagCheckItem: 编辑后的最新检查结果。

    Raises:
        HTTPException: 插入位置或检查位置非法、标签为空、图片不存在时返回 400/404。

    Callers:
        - ``static/js/tag_check.js:onCardClick``
        - ``static/js/tag_check.js:singleTagOp``
    """
    rt = ds
    root = rt.root
    if body.position not in TAG_INSERT_POSITIONS:
        raise HTTPException(400, f"position 非法: {body.position}")
    if body.check_position not in TAG_CHECK_POSITIONS:
        raise HTTPException(400, f"check_position 非法: {body.check_position}")
    if not body.tag.strip():
        raise HTTPException(400, "标签不能为空")

    tag = body.tag.strip()
    img_abs = resolve_path(rt, body.path)
    if not img_abs.is_file():
        raise HTTPException(404, "图片不存在")

    txt_path = img_abs.with_suffix(".txt")
    raw = ""
    if txt_path.is_file():
        raw = txt_path.read_text(encoding="utf-8")

    new_caption = add_tag_to_caption(raw, tag, body.position)
    atomic_write_text(txt_path, new_caption + "\n")

    rel = body.path.replace("\\", "/").lstrip("/")
    work = rt.work_of(rel)
    logger.info(
        "单图加标签 dataset=%s path=%s tag=%s position=%s",
        rt.name,
        rel,
        tag,
        body.position,
    )
    return _build_check_item(work, rel, rt.thumbnailer, tag, body.check_position)


class TagDeleteOneBody(BaseModel):
    """单文件标签删除请求."""

    path: str = Field(..., description="图片相对数据集根的路径")
    tag: str


@router.post("/delete-one")
def delete_one(ds: DatasetDep, body: TagDeleteOneBody) -> TagCheckItem:
    """从单张图片旁路 ``.txt`` 中删除指定标签后返回最新检查结果。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (TagDeleteOneBody): 删除参数, 含路径、标签。

    Returns:
        TagCheckItem: 删除后的最新检查结果。

    Callers:
        - ``static/js/tag_check.js:onCardClick``
        - ``static/js/tag_check.js:singleTagOp``
    """
    rt = ds
    root = rt.root
    if not body.tag.strip():
        raise HTTPException(400, "标签不能为空")

    tag = body.tag.strip()
    img_abs = resolve_path(rt, body.path)
    if not img_abs.is_file():
        raise HTTPException(404, "图片不存在")

    txt_path = img_abs.with_suffix(".txt")
    raw = ""
    if txt_path.is_file():
        raw = txt_path.read_text(encoding="utf-8")

    new_caption = delete_tag_from_caption(raw, tag)
    atomic_write_text(txt_path, new_caption + "\n")

    rel = body.path.replace("\\", "/").lstrip("/")
    work = rt.work_of(rel)
    logger.info(
        "单图删标签 dataset=%s path=%s tag=%s",
        rt.name,
        rel,
        tag,
    )
    return _build_check_item(work, rel, rt.thumbnailer, tag, "any")


# ==================== 向量重排序 API ====================


@router.post("/embedding/status", response_model=EmbeddingStatusResponse)
def embedding_status() -> EmbeddingStatusResponse:
    """检查向量重排序模型是否可用。

    Returns:
        EmbeddingStatusResponse: 含 ok 与 message 字段。

    Callers:
        - ``static/js/tag_check.js:embSort``
    """
    ok, msg = reranker_model_ready()
    return EmbeddingStatusResponse(ok=ok, message=msg)


@router.post("/embedding/query")
async def embedding_query(ds: DatasetDep, body: EmbeddingQueryBody) -> JobSubmitResponse:
    """按查询文本对候选图片实时重排序(异步长任务, 带进度条)。

    Reranker 为 cross-encoder, 无法预计算向量, 须对目录内全部图片逐张
    (query, image) 评分, 耗时较长, 故提交 JobManager, 由前端以 SSE 订阅进度,
    任务完成时在 result 中携带排序结果。

    同一数据集最近 ``MAX_ENTRIES`` 次查询结果缓存于 ``Dataset.sort_cache``：
    命中(查询文本与目录集合一致)时任务立即返回缓存排序, 无需重复调用模型；
    命中即提升该查询在最近列表中的位置, 避免被后续查询挤出。
    缓存排序之后新增的图片会重新调用模型评分, 按相似度并入结果并回写缓存,
    保证返回结果覆盖当前全部候选。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (EmbeddingQueryBody): 查询参数, 含查询文本与目录列表。

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度。

    Callers:
        - ``static/js/tag_check.js:embSort``
        - ``static/js/app.js:searchSort``
    """
    rt = ds
    root = rt.root
    works = rt.select(body.dirs)
    if not works:
        raise HTTPException(400, "请选择目录")
    if not body.query.strip():
        raise HTTPException(400, "查询文本不能为空")

    query_text = body.query.strip()
    dirs_key = tuple(sorted((w.name for w in works), key=natural_key))
    cache = rt.sort_cache
    cached = cache.get(query_text, dirs_key)

    # 收集所有候选图片的相对路径与绝对路径(命中缓存时用于补全, 未命中时用于评分)
    candidates: list[tuple[str, Path]] = [
        (img.rel, img.abs) for work in works for img in work.images()
    ]

    if cached is not None:
        # 找出缓存排序之后新增的图片, 命中时重新调用模型评分后并入结果
        ordered_paths = {p for p, _ in cached}
        missing = [c for c in candidates if c[0] not in ordered_paths]

        def run(emit: EmitFn) -> EmbeddingQueryResult:
            """工作线程: 命中缓存, 新图重新评分后按相似度并入并更新缓存。

            Args:
                emit (EmitFn): 任务事件推送回调。

            Returns:
                EmbeddingQueryResult: 排序结果。

            Callers:
                - ``services/core/jobs.py:JobManager.submit``
            """
            if missing:
                emit(JobSnapshot(
                    status="running", phase="querying",
                    message=f"正在补全 {len(missing)} 张新图评分…", pct=0, total=len(missing),
                ))

                def on_progress(done: int, n_total: int) -> None:
                    emit(JobSnapshot(
                        status="running", phase="querying",
                        message=f"正在补全评分 {done}/{n_total}…",
                        pct=round(done / n_total * 100, 1) if n_total else 100,
                        done=done, total=n_total,
                    ))

                new_hits = rerank(query_text, missing, on_progress=on_progress)
                hits = sorted(cached + new_hits, key=lambda x: x[1], reverse=True)
                cache.put(query_text, dirs_key, hits)
            else:
                hits = cached

            items = [
                EmbeddingQueryItem(path=p, stem=Path(p).stem, similarity=round(s, 6))
                for p, s in hits
            ]
            result = EmbeddingQueryResult(items=items)
            emit(JobSnapshot(
                status="done", phase="done",
                message=f"命中缓存, 共 {len(items)} 张", pct=100, result=result,
            ))
            return result

        if missing:
            ok, msg = reranker_model_ready()
            if not ok:
                raise HTTPException(400, msg)

        job_id = await jobs(request).submit(
            kind="embedding_query", group="compute", dataset=rt.name, run=run
        )
        logger.info(
            "语义排序-缓存命中 dataset=%s query=%s 目录数=%d 新图=%d job_id=%s",
            rt.name,
            query_text,
            len(selected),
            len(missing),
            job_id,
        )
        return JobSubmitResponse(ok=True, job_id=job_id)

    ok, msg = reranker_model_ready()
    if not ok:
        raise HTTPException(400, msg)

    total = len(candidates)

    def run(emit: EmitFn) -> EmbeddingQueryResult:
        """工作线程: 逐张相似度评分、写入缓存并回报进度。

        Args:
            emit (EmitFn): 任务事件推送回调。

        Returns:
            EmbeddingQueryResult: 排序结果。

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        emit(JobSnapshot(
            status="running", phase="querying",
            message="正在相似度评分…", pct=0, total=total,
        ))

        def on_progress(done: int, n_total: int) -> None:
            emit(JobSnapshot(
                status="running", phase="querying",
                message=f"正在相似度评分 {done}/{n_total}…",
                pct=round(done / n_total * 100, 1) if n_total else 100,
                done=done, total=n_total,
            ))

        hits = rerank(query_text, candidates, on_progress=on_progress)
        cache.put(query_text, dirs_key, hits)
        items = [
            EmbeddingQueryItem(path=p, stem=Path(p).stem, similarity=round(s, 6))
            for p, s in hits
        ]
        result = EmbeddingQueryResult(items=items)
        emit(JobSnapshot(
            status="done", phase="done",
            message=f"完成, 共 {len(items)} 张", pct=100, result=result,
        ))
        return result

    job_id = await jobs(request).submit(kind="embedding_query", group="compute", dataset=rt.name, run=run)
    logger.info(
        "语义排序 dataset=%s query=%s 目录数=%d 候选图片=%d job_id=%s",
        rt.name,
        query_text,
        len(selected),
        total,
        job_id,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)


@router.get("/embedding/history", response_model=EmbeddingHistoryResponse)
def embedding_history(ds: DatasetDep) -> EmbeddingHistoryResponse:
    """返回当前数据集最近若干次语义排序查询历史(新的在前, 至多 10 条)。

    Args:
        ds (DatasetDep): 当前数据集运行时依赖。

    Returns:
        EmbeddingHistoryResponse: 含 query 与 time 的历史列表。

    Callers:
        - ``static/js/app.js:searchLoadHistory``
    """
    rt = ds
    return EmbeddingHistoryResponse(
        queries=[
            EmbeddingHistoryItem(query=q, time=t)
            for q, t in rt.sort_cache.recent()
        ]
    )


@router.post("/embedding/batch-tag", response_model=EmbeddingBatchTagResponse)
def embedding_batch_tag(ds: DatasetDep, body: EmbeddingBatchTagBody) -> EmbeddingBatchTagResponse:
    """按已计算相似度的路径批量添加或删除标签(同步)。

    相似度由 ``embedding/query`` 实时评分得出并缓存于前端, 本接口直接
    接收前端按阈值筛选出的匹配路径, 仅做批量写标签, 开销极小, 故不采用长任务调度。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (EmbeddingBatchTagBody): 批量操作参数, 含路径列表、标签、动作、位置。

    Returns:
        EmbeddingBatchTagResponse: 含 ok 与匹配数量。

    Callers:
        - ``static/js/tag_check.js:embBatchTag``
    """
    root = ds.root
    if not body.paths:
        raise HTTPException(400, "无匹配图片, 请先按相似度排序")
    if not body.tag.strip():
        raise HTTPException(400, "标签不能为空")
    if body.action not in ("add", "delete"):
        raise HTTPException(400, "action 须为 add 或 delete")

    tag = body.tag.strip()
    action = body.action
    position = body.position
    fuzzy = body.fuzzy

    changed = 0
    for rel in body.paths:
        img_abs = resolve_path(rt, rel)
        if not img_abs.is_file():
            continue
        txt_path = img_abs.with_suffix(".txt")
        raw = txt_path.read_text(encoding="utf-8") if txt_path.is_file() else ""

        if action == "add":
            new_caption = add_tag_to_caption(raw, tag, position)
        elif fuzzy:
            new_caption = delete_tag_from_caption_fuzzy(raw, tag)
        else:
            new_caption = delete_tag_from_caption(raw, tag)
        atomic_write_text(txt_path, new_caption + "\n")
        changed += 1

    logger.info(
        "批量打标 dataset=%s action=%s tag=%s 匹配=%d 变更=%d",
        request.state.runtime.root.name,
        action,
        tag,
        len(body.paths),
        changed,
    )
    return EmbeddingBatchTagResponse(
        ok=True,
        matched_count=changed,
        message=f"已{'添加' if action == 'add' else '删除'}标签 {tag} 到 {changed} 张图片",
    )


def delete_tag_from_caption_fuzzy(raw: str, tag: str) -> str:
    """从 caption 中按子串模糊匹配移除标签。

    Args:
        raw (str): 原始 caption 文本。
        tag (str): 待删除的标签子串(不区分大小写)。

    Returns:
        str: 删除后的 caption 字符串。

    Callers:
        - ``routes/tag_stats.py:embedding_batch_tag``
    """
    header, body, has_sep = split_header_body(raw)
    tag_lower = tag.lower()
    header = [t for t in header if tag_lower not in t.lower()]
    body = [t for t in body if tag_lower not in t.lower()]
    if body:
        return join_tags_line(header) + " ||| " + join_tags_line(body)
    return join_tags_line(header)


@router.post("/concept/query")
async def concept_query(ds: DatasetDep, body: ConceptQueryBody) -> JobSubmitResponse:
    """按概念提示词对候选图片执行文本提示概念分割(异步长任务, 带进度条)。

    SAM3 对每张图片逐张分割并计算掩码占比, 耗时较长, 故提交 JobManager,
    由前端以 SSE 订阅进度；任务完成时在 result 中携带按掩码占比降序的结果
    及每张图的半透明叠加 PNG(base64)。

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入。
        body (ConceptQueryBody): 概念查询参数, 含查询文本与目录列表。

    Returns:
        JobSubmitResponse: 含 job_id, 前端据以订阅 SSE 进度。

    Callers:
        - ``static/js/tag_check.js:embSort``
    """
    rt = ds
    root = rt.root
    works = rt.select(body.dirs)
    if not works:
        raise HTTPException(400, "请选择目录")

    if not body.query.strip():
        raise HTTPException(400, "概念提示词不能为空")

    ok, msg = concept_model_ready()
    if not ok:
        raise HTTPException(400, msg)

    candidates: list[tuple[str, Path]] = [
        (img.rel, img.abs) for work in works for img in work.images()
    ]

    concept = body.query.strip()
    total = len(candidates)

    def run(emit: EmitFn) -> ConceptQueryResult:
        """工作线程: 逐张概念分割并回报进度。

        Args:
            emit (EmitFn): 任务事件推送回调。

        Returns:
            ConceptQueryResult: 概念分割结果。

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        emit(JobSnapshot(
            status="running", phase="concept",
            message="正在概念分割…", pct=0, total=total,
        ))

        def on_progress(done: int, n_total: int) -> None:
            emit(JobSnapshot(
                status="running", phase="concept",
                message=f"正在概念分割 {done}/{n_total}…",
                pct=round(done / n_total * 100, 1) if n_total else 100,
                done=done, total=n_total,
            ))

        hits = run_concept_query(candidates, concept, on_progress=on_progress)
        items = [
            ConceptQueryItem(
                path=it["path"],
                ratio=round(it["ratio"], 6),
                mask_data=it["mask_data"],
            )
            for it in hits
        ]
        result = ConceptQueryResult(items=items)
        emit(JobSnapshot(
            status="done", phase="done",
            message=f"完成, 共 {len(items)} 张", pct=100, result=result,
        ))
        return result

    job_id = await jobs(request).submit(kind="concept_query", group="compute", dataset=rt.name, run=run)
    logger.info(
        "概念分割查询 dataset=%s query=%s 目录数=%d 候选图片=%d job_id=%s",
        rt.name,
        concept,
        len(selected),
        total,
        job_id,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)
