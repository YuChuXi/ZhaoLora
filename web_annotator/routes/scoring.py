"""统一评分 API(美学 + 质量), 数据集作用域. """

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from routes.deps import DatasetDep, jobs
from services.core.jobs import EmitFn
from services.analysis.scoring import (
    ScoreProgress,
    build_score_results,
    run_scoring,
)
from services.models.pages import (
    ScoringDirsResponse,
    ScoringStartResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scoring", tags=["scoring"])


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

class StartBody(BaseModel):
    """评分启动请求. 

    Callers:
        - ``routes/scoring.py:start`` : POST /scoring/start 端点请求体.
    """

    kind: str = Field(default="aesthetic", description="评分类型: 'aesthetic' | 'quality'")
    dirs: list[str] = Field(default_factory=list)
    write_report: bool = True


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------

@router.get("/dirs")
def dirs(ds: DatasetDep) -> ScoringDirsResponse:
    """列出可评分目录与两类分数文件的就绪状态. 

    Callers:
        - ``routes/scoring.py:`` : 页面初始化时加载. 
    """
    from services.analysis.scoring import load_score_json, AESTHETIC_SCORES, QUALITY_SCORES

    aes_scores = load_score_json(ds.root, AESTHETIC_SCORES)
    qual_scores = load_score_json(ds.root, QUALITY_SCORES)

    return ScoringDirsResponse(
        dirs=[w.to_info() for w in ds.work_dirs()],
        aesthetic_ready=len(aes_scores) > 0,
        aesthetic_count=len(aes_scores),
        quality_ready=len(qual_scores) > 0,
        quality_count=len(qual_scores),
    )


@router.get("/results")
async def results(
    ds: DatasetDep,
    kind: str = Query(..., description="评分类型: 'aesthetic' | 'quality'"),
):
    """返回评分结果 JSON(含缩略图 URL), 前端直接渲染画廊. 

    Args:
        kind: 评分类型. 

    Callers:
        - ``routes/scoring.py:`` : 获取评分数据渲染画廊. 
    """
    if kind not in ("aesthetic", "quality"):
        raise HTTPException(400, "kind 必须是 'aesthetic' 或 'quality'")
    from urllib.parse import quote

    results = build_score_results(ds.root, kind)
    name = ds.name

    for r in results:
        p = r["path"]
        encoded = quote(p, safe="/")
        r["image_url"] = f"/api/d/{name}/image?path={encoded}"
        r["thumb_url"] = f"/d/{name}/thumb/{ds.thumbnailer.hash_for(p)}.avif"
        await ds.thumbnailer.ensure_async(p)

    return {
        "kind": kind,
        "total": len(results),
        "results": results,
    }


@router.post("/start")
async def start(ds: DatasetDep, request: Request, body: StartBody) -> ScoringStartResponse:
    """提交评分长任务(美学或质量). 

    返回 job_id；进度通过 GET /api/jobs/{id}/events (SSE) 获取. 

    Callers:
        - ``routes/scoring.py:`` : 用户点击"开始"按钮. 
    """
    kind = body.kind
    if kind not in ("aesthetic", "quality"):
        raise HTTPException(400, "kind 必须是 'aesthetic' 或 'quality'")

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
    logger.info(
        "评分任务启动 dataset=%s kind=%s dirs=%d",
        ds.name,
        kind,
        len(works),
    )
    return ScoringStartResponse(
        ok=True,
        job_id=job_id,
        kind=kind,
        dirs=[w.name for w in works],
    )
