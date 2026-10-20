"""去重 API: 扫描重复组 / 删除选中图(数据集作用域). """

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import DatasetDep, jobs
from services.analysis.dedupe import (
    extract_fft_features,
    extract_orb_features,
    find_duplicate_groups,
    load_records,
)
from services.core.jobs import EmitFn
from services.models import (
    DedupeMoveResponse,
    DedupeScanResult,
    DirsResponse,
    JobSnapshot,
    JobSubmitResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dedupe", tags=["dedupe"])


class ScanBody(BaseModel):
    """去重扫描请求体. """

    dirs: list[str] = Field(default_factory=lambda: ["main"])
    dir: str | None = None
    algorithm: str = Field(default="mse", description="mse | fft | orb")
    mse_threshold: float = Field(default=1e-4, gt=0, le=1)
    sim_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    similar_res_ratio: float = Field(default=1.05, ge=1.0, le=10.0)


class ApplyBody(BaseModel):
    """去重删除请求体. """

    paths: list[str]
    dirs: list[str] = Field(default_factory=lambda: ["main"])
    dir: str | None = None


def similarity_histogram(values, algorithm: str) -> str | None:
    """两两相似度直方图, 以 base64 data URL 返回(MSE 用对数横轴).

    依赖 matplotlib (Agg 后端). matplotlib 缺失或无可绘制数据时返回 None.

    Args:
        values (list[float] | np.ndarray): 两两相似度值数组.
        algorithm (str): 相似度算法, ``"mse"`` / ``"fft"`` / ``"orb"``.

    Returns:
        str | None: ``data:image/png;base64,...`` 形式的 data URL; 无可绘制数据或 matplotlib 缺失时返回 None.

    Callers:
        - ``routes/dedupe.py:scan_duplicates`` (内部 ``run`` 闭包, 用于扫描结果直方图)
    """
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = [
            "Microsoft YaHei",
            "WenQuanYi Zen Hei",
            "Noto Sans CJK SC",
            "SimHei",
            "DejaVu Sans",
        ]
        plt.rcParams["axes.unicode_minus"] = False
    except ImportError:
        return None

    import base64
    import io

    import numpy as np

    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return None

    fig, ax = plt.subplots(figsize=(9, 5))
    if algorithm in ("mse", "orb"):
        v = vals[vals > 0]
        if v.size == 0:
            plt.close(fig)
            return None
        bins = np.logspace(np.log10(v.min()), np.log10(v.max()), 100)
        ax.hist(v, bins=bins, color="#777777", edgecolor="#111111")
        ax.set_xscale("log")
        if algorithm == "mse":
            ax.set_xlabel("MSE (log)")
            title = "MSE 两两相似度分布 (log)"
        else:
            ax.set_xlabel("匹配比例 (log)")
            title = "ORB 匹配比例分布 (log)"
    else:
        ax.hist(vals, bins=100, color="#777777", edgecolor="#111111")
        ax.set_xlabel("相似度")
        title = "FFT 余弦相似度分布"
    ax.set_yscale("log")
    ax.set_ylabel("数量 (log)")
    ax.set_title(title)
    ax.text(
        0.02,
        0.95,
        f"N={vals.size}  median={np.median(vals):.4g}  p95={np.quantile(vals, 0.95):.4g}",
        transform=ax.transAxes,
        va="top",
        fontsize=9,
    )
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=140)
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@router.get("/dirs")
def dirs(ds: DatasetDep) -> DirsResponse:
    """列出可扫描工作目录, 默认 ``main``.

    Args:
        ds (Dataset): 当前数据集, 由路由依赖注入.

    Returns:
        DirsResponse: 含工作目录列表与默认目录名.

    Callers:
        - ``static/js/nav.js:dirsApi``
    """
    return DirsResponse(dirs=[w.to_info() for w in ds.work_dirs()], default="main")


@router.post("/scan")
async def scan_duplicates(request: Request, ds: DatasetDep, body: ScanBody) -> JobSubmitResponse:
    """提交去重扫描长任务, 返回 job_id.

    扫描任务结束事件的 ``result`` 字段含分组结果(DedupeScanResult).
    支持三种算法: ``mse``(默认)、``fft``、``orb``; 后两者需先提取特征再比较相似度.

    Args:
        request (Request): FastAPI 请求对象, 由路由参数注入.
        ds (Dataset): 当前数据集, 由路由依赖注入.
        body (ScanBody): 扫描参数, 含目录/算法/阈值/分辨率比.

    Returns:
        JobSubmitResponse: 含 ``job_id``, 前端经 SSE 监听任务进度与最终结果.

    Callers:
        - ``static/js/dedupe.js:scan``
    """
    thumbnailer = ds.thumbnailer
    names = body.dirs or ([body.dir] if body.dir else None)
    works = ds.select_or_main(names)

    mse = body.mse_threshold
    ratio = body.similar_res_ratio
    algorithm = "mse" if body.algorithm not in ("mse", "fft", "orb") else body.algorithm
    sim = body.sim_threshold

    def run(emit: EmitFn) -> DedupeScanResult:
        """工作线程: 扫描重复组, 逐阶段上报进度.

        Args:
            emit (EmitFn): 任务进度发射回调, 由 ``JobManager`` 注入.

        Returns:
            DedupeScanResult: 重复组列表与统计, 同时通过 ``emit`` 发送 done 快照.

        Callers:
            - ``routes/dedupe.py:scan_duplicates`` (作为 ``run`` 参数传入 ``services.core.jobs.JobManager.submit``)
        """
        emit(
            JobSnapshot(
                status="running",
                phase="scanning",
                message="扫描中…",
                pct=0,
            )
        )
        records = []
        for work in works:
            part = load_records(work.path)
            for r in part:
                r.rel = f"{work.name}/{r.rel}" if not r.rel.startswith(work.name + "/") else r.rel
            records.extend(part)

        if algorithm in ("fft", "orb"):
            from services.analysis.scoring import ScoreProgress

            feat_p = ScoreProgress(
                status="running",
                phase=algorithm,
                total=len(records),
                message="提取 " + ("FFT" if algorithm == "fft" else "ORB") + " 特征中…",
            )

            def on_step(done: int, total: int) -> None:
                """特征提取逐张进度上报回调.

                Args:
                    done (int): 已完成特征提取的图片数.
                    total (int): 总图片数.

                Callers:
                    - ``services/analysis/dedupe.py:extract_fft_features`` (作为 ``on_step`` 回调)
                    - ``services/analysis/dedupe.py:extract_orb_features`` (作为 ``on_step`` 回调)
                """
                feat_p.done = done
                if done % 10 == 0 or done == total:
                    emit(feat_p.snapshot())

            emit(feat_p.snapshot())
            if algorithm == "fft":
                extract_fft_features(records, progress=feat_p, on_step=on_step)
            else:
                extract_orb_features(records, progress=feat_p, on_step=on_step)

        emit(
            JobSnapshot(
                status="running",
                phase="comparing",
                message="比较相似度中…",
                pct=None,
            )
        )
        groups, vals = find_duplicate_groups(
            records,
            algorithm=algorithm,
            mse_threshold=mse,
            sim_threshold=sim,
            similar_res_ratio=ratio,
        )
        for g in groups:
            for m in g.members:
                m.thumbnail_hash = thumbnailer.hash_for(m.path)
                if not thumbnailer.exists(m.path):
                    thumbnailer.generate(m.path)

        histogram = similarity_histogram(vals, algorithm)

        result = DedupeScanResult(
            dirs=[w.name for w in works],
            dir=works[0].name,
            image_count=len(records),
            group_count=len(groups),
            duplicate_image_count=sum(g.size for g in groups),
            algorithm=algorithm,
            mse_threshold=mse,
            sim_threshold=sim,
            similar_res_ratio=ratio,
            groups=groups,
            histogram=histogram,
        )
        emit(
            JobSnapshot(
                status="done",
                phase="done",
                message=f"完成 {len(groups)} 组",
                pct=100,
                result=result,
            )
        )
        return result

    job_id = await jobs(request).submit(kind="dedupe_scan", group="compute", dataset=ds.name, run=run)
    logger.info(
        "去重扫描任务提交 dataset=%s algorithm=%s dirs=%s",
        ds.name,
        algorithm,
        names,
    )
    return JobSubmitResponse(ok=True, job_id=job_id)


@router.post("/apply")
def apply_delete(ds: DatasetDep, body: ApplyBody) -> DedupeMoveResponse:
    """将手选路径移入回收站(与筛选页同一套 trash 机制).

    仅删除当前 ``body.dirs`` 中实际存在的图片; 忽略非法路径与未在存活集合中的路径.
    删除成功后清空色彩索引缓存, 等待下次访问重建.

    Args:
        ds (Dataset): 当前数据集, 由路由依赖注入.
        body (ApplyBody): 含待删除路径列表与目录字段.

    Returns:
        DedupeMoveResponse: 实际删除的图片数、路径列表与目录列表.

    Raises:
        HTTPException: 路径列表为空或全部非法时返回 400.

    Callers:
        - ``static/js/dedupe.js:applyDelete``
    """
    state_mgr = ds.state_manager
    if not body.paths:
        raise HTTPException(400, "未选择任何图片")

    names = _names(body.dirs, body.dir)
    works = ds.select_or_main(names)

    alive: set[str] = set()
    for work in works:
        alive |= {img.rel for img in work.images()}

    paths: list[str] = []
    seen: set[str] = set()
    for p in body.paths:
        rel = (p or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/") or rel in seen:
            continue
        if rel not in alive:
            continue
        seen.add(rel)
        paths.append(rel)

    if not paths:
        raise HTTPException(400, "没有可删除的有效路径")

    moved = state_mgr.apply_delete(paths)
    idx = ds.color_index
    if idx is not None:
        idx.clear()

    logger.info(
        "去重删除完成 dataset=%s moved=%d count=%d",
        ds.name,
        moved,
        len(paths),
    )
    return DedupeMoveResponse(
        moved=moved,
        paths=paths,
        dirs=[w.name for w in works],
    )
