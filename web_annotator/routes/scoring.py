"""统一评分 API(美学 + WD14 数据驱动评分), 数据集作用域. """

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from routes.deps import DatasetDep, jobs
from services.analysis.scoring import (
    AESTHETIC_SCORES,
    ScoreProgress,
    build_score_results,
    create_score_item,
    delete_score_item,
    load_item_scores,
    load_score_items,
    load_score_json,
    run_scoring,
)
from services.analysis.wd14 import (
    TaggingProgress,
    Wd14Cache,
    Wd14Labels,
    build_cache,
    model_ready,
)
from services.core.jobs import EmitFn
from services.models import (
    ScoreItemBody,
    ScoreItemsResponse,
    ScoringDirsResponse,
    ScoringStartResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scoring", tags=["scoring"])

# 可提交的评分任务类型: 美学模型评分 / 按 WD14 缓存计算全部评分项
SCORE_KINDS = ("aesthetic", "wd14")


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class StartBody(BaseModel):
    """评分启动请求. 

    Callers:
        - ``routes/scoring.py:start`` : POST /scoring/start 端点请求体.
    """

    kind: str = Field(default="aesthetic", description="评分类型: 'aesthetic' | 'wd14'")
    dirs: list[str] = Field(default_factory=list)


class CacheBody(BaseModel):
    """WD14 缓存建立请求.

    Callers:
        - ``routes/scoring.py:start_cache`` : POST /scoring/wd14 端点请求体.
    """

    dirs: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@router.get("/dirs")
def dirs(ds: DatasetDep) -> ScoringDirsResponse:
    """列出可评分目录、评分项与两类评分数据的就绪状态.

    Args:
        ds: 当前数据集, 由路由依赖注入.

    Returns:
        ScoringDirsResponse: 目录列表、美学分数数量、评分项及各项目前已算出的数量、
            WD14 缓存覆盖率与模型状态.

    Callers:
        - ``static/js/scoring.js:initPage`` : 页面初始化时加载.
    """
    aesthetic = load_score_json(ds.root, AESTHETIC_SCORES)
    items = load_score_items(ds.root)
    cached = load_item_scores(ds.root)
    ok, message = model_ready()
    works = ds.work_dirs()
    images = [p for work in works for p in work.image_files()]
    cached_count = 0
    if ok:
        pending = Wd14Cache(ds.root, Wd14Labels.load()).missing(images)
        cached_count = len(images) - len(pending)
    return ScoringDirsResponse(
        dirs=[w.to_info() for w in works],
        aesthetic_ready=bool(aesthetic),
        aesthetic_count=len(aesthetic),
        items=items,
        item_counts={item.key: len(cached.get(item.key, {})) for item in items},
        wd14_ready=ok,
        wd14_message=message,
        wd14_count=cached_count,
        wd14_total=len(images),
    )


@router.get("/items")
def items(ds: DatasetDep) -> ScoreItemsResponse:
    """返回数据集当前的评分项列表.

    Args:
        ds: 当前数据集, 由路由依赖注入.

    Returns:
        ScoreItemsResponse: 内置项在前、自定义项在后.

    Callers:
        - ``static/js/scoring.js:loadItems``
    """
    return ScoreItemsResponse(items=load_score_items(ds.root))


@router.post("/items")
def create_item(ds: DatasetDep, body: ScoreItemBody) -> ScoreItemsResponse:
    """新增一个自定义评分项.

    表达式中的双引号字符串引用同名 WD14 标签概率, 保存前校验语法与标签是否存在.

    Args:
        ds: 当前数据集, 由路由依赖注入.
        body: 评分项名称与表达式.

    Returns:
        ScoreItemsResponse: 更新后的评分项列表.

    Raises:
        HTTPException: WD14 模型未就绪或表达式非法时返回 400.

    Callers:
        - ``static/js/scoring.js:createItem``
    """
    ok, message = model_ready()
    if not ok:
        raise HTTPException(400, message)
    label_index = Wd14Labels.load().index()
    try:
        result = create_score_item(
            ds.root, label=body.label, expr=body.expr, label_index=label_index,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return ScoreItemsResponse(items=result)


@router.delete("/items/{key}")
def delete_item(ds: DatasetDep, key: str) -> ScoreItemsResponse:
    """删除一个自定义评分项及其已缓存的分数.

    Args:
        ds: 当前数据集, 由路由依赖注入.
        key: 评分项 key.

    Returns:
        ScoreItemsResponse: 更新后的评分项列表.

    Raises:
        HTTPException: 评分项不存在或为内置项时返回 404 / 400.

    Callers:
        - ``static/js/scoring.js:deleteItem``
    """
    try:
        result = delete_score_item(ds.root, key)
    except KeyError as e:
        raise HTTPException(404, "评分项不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return ScoreItemsResponse(items=result)


@router.get("/results")
async def results(
    ds: DatasetDep,
    kind: str = Query(..., description="'aesthetic' 或评分项 key"),
):
    """返回评分结果 JSON(含缩略图 URL), 前端直接渲染画廊. 

    Args:
        ds: 当前数据集, 由路由依赖注入.
        kind: ``aesthetic`` 或评分项 key. 

    Returns:
        dict: 评分结果列表与总数.

    Raises:
        HTTPException: 评分项不存在时返回 400.

    Callers:
        - ``static/js/scoring.js:loadResults`` : 获取评分数据渲染画廊. 
    """
    if kind != "aesthetic" and kind not in {item.key for item in load_score_items(ds.root)}:
        raise HTTPException(400, f"未知评分项: {kind}")

    results = build_score_results(ds.root, kind)
    name = ds.name

    for r in results:
        p = r["path"]
        r["image_url"] = f"/api/d/{name}/image?path={quote(p, safe='/')}"
        r["thumb_url"] = f"/d/{name}/thumb/{ds.thumbnailer.hash_for(p)}.avif"
        await ds.thumbnailer.ensure_async(p)

    return {"kind": kind, "total": len(results), "results": results}


@router.post("/start")
async def start(ds: DatasetDep, request: Request, body: StartBody) -> ScoringStartResponse:
    """提交评分长任务(美学模型评分或全部评分项计算). 

    返回 job_id；进度通过 GET /api/jobs/{id}/events (SSE) 获取. 

    Args:
        ds: 当前数据集, 由路由依赖注入.
        request: FastAPI 请求对象.
        body: 评分类型与所选目录.

    Returns:
        ScoringStartResponse: 任务提交回执.

    Raises:
        HTTPException: 评分类型非法或未选择目录时返回 400.

    Callers:
        - ``static/js/scoring.js:startJob``
    """
    kind = body.kind
    if kind not in SCORE_KINDS:
        raise HTTPException(400, "kind 必须是 'aesthetic' 或 'wd14'")

    works = ds.select(body.dirs)
    if not works:
        raise HTTPException(400, "请选择目录")

    prog = ScoreProgress()

    def run(emit: EmitFn) -> None:
        """工作线程入口. 

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        def on_update() -> None:
            emit(prog.snapshot())

        run_scoring(
            works,
            kind=kind,
            progress=prog,
            on_update=on_update,
        )
        on_update()

    job_id = await jobs(request).submit(
        kind=kind,
        group="compute",
        dataset=ds.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    logger.info("评分任务启动 dataset=%s kind=%s dirs=%d", ds.name, kind, len(works))
    return ScoringStartResponse(
        ok=True,
        job_id=job_id,
        kind=kind,
        dirs=[w.name for w in works],
    )


@router.post("/wd14")
async def start_cache(
    ds: DatasetDep, request: Request, body: CacheBody,
) -> ScoringStartResponse:
    """提交 WD14 推理缓存建立长任务.

    只补齐缓存, 不改动图片旁的标签文件; 缓存完成后才能在评分页计算评分项.

    Args:
        ds: 当前数据集, 由路由依赖注入.
        request: FastAPI 请求对象.
        body: 所选目录.

    Returns:
        ScoringStartResponse: 任务提交回执.

    Raises:
        HTTPException: WD14 模型未就绪或未选择目录时返回 400.

    Callers:
        - ``static/js/scoring.js:startCache``
    """
    ok, message = model_ready()
    if not ok:
        raise HTTPException(400, message)

    works = ds.select(body.dirs)
    if not works:
        raise HTTPException(400, "请选择目录")

    prog = TaggingProgress()

    def run(emit: EmitFn) -> None:
        """工作线程入口: 补齐 WD14 推理缓存.

        Args:
            emit: 任务进度发射回调.

        Callers:
            - ``services/core/jobs.py:JobManager.submit``
        """
        def on_update() -> None:
            emit(prog.snapshot())

        build_cache(works, progress=prog, on_update=on_update)
        on_update()

    job_id = await jobs(request).submit(
        kind="wd14_cache",
        group="compute",
        dataset=ds.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    logger.info(
        "WD14 缓存任务启动 dataset=%s dirs=%s job_id=%s",
        ds.name,
        [w.name for w in works],
        job_id,
    )
    return ScoringStartResponse(
        ok=True,
        job_id=job_id,
        kind="wd14_cache",
        dirs=[w.name for w in works],
    )
