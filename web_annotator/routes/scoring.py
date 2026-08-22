"""统一评分 API(美学 + 质量), 数据集作用域. """

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs
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
from services.analysis.wd14 import list_image_subdirs, resolve_image_dirs

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
def dirs(request: Request) -> ScoringDirsResponse:
    """列出可评分目录与两类分数文件的就绪状态. 

    Callers:
        - ``routes/scoring.py:`` : 页面初始化时加载. 
    """
    rt = get_runtime(request)
    root = rt.root
    from services.analysis.scoring import load_score_json, AESTHETIC_SCORES, QUALITY_SCORES

    aes_scores = load_score_json(root, AESTHETIC_SCORES)
    qual_scores = load_score_json(root, QUALITY_SCORES)

    return ScoringDirsResponse(
        dirs=list_image_subdirs(root),
        aesthetic_ready=len(aes_scores) > 0,
        aesthetic_count=len(aes_scores),
        quality_ready=len(qual_scores) > 0,
        quality_count=len(qual_scores),
    )


@router.get("/results")
async def results(
    request: Request,
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

    rt = get_runtime(request)
    results = build_score_results(rt.root, kind)
    ds = rt.name

    for r in results:
        p = r["path"]
        encoded = quote(p, safe="/")
        r["image_url"] = f"/api/d/{ds}/image?path={encoded}"
        r["thumb_url"] = f"/d/{ds}/thumb/{rt.thumbnailer.hash_for(p)}.avif"
        await rt.thumbnailer.ensure_async(p)

    return {
        "kind": kind,
        "total": len(results),
        "results": results,
    }


@router.post("/start")
async def start(request: Request, body: StartBody) -> ScoringStartResponse:
    """提交评分长任务(美学或质量). 

    返回 job_id；进度通过 GET /api/jobs/{id}/events (SSE) 获取. 

    Callers:
        - ``routes/scoring.py:`` : 用户点击"开始"按钮. 
    """
    kind = body.kind
    if kind not in ("aesthetic", "quality"):
        raise HTTPException(400, "kind 必须是 'aesthetic' 或 'quality'")

    rt = get_runtime(request)
    root = rt.root
    selected = resolve_image_dirs(root, body.dirs)
    if not selected:
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
            selected,
            kind=kind,
            root=root,
            progress=prog,
            on_update=on_update,
        )
        on_update()

    job_id = await jobs(request).submit(
        kind=kind,
        group="compute",
        dataset=rt.name,
        run=run,
        cancel_fn=lambda: setattr(prog, "cancel_requested", True),
    )
    return ScoringStartResponse(
        ok=True,
        job_id=job_id,
        kind=kind,
        dirs=[p.name if p != root else "." for p in selected],
    )
