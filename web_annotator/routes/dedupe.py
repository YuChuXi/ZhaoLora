"""去重 API: 扫描重复组 / 删除选中图(数据集作用域). """

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes.deps import get_runtime, jobs
from services.analysis.dedupe import (
    extract_fft_features,
    extract_orb_features,
    find_duplicate_groups,
    load_records,
    pairwise_values,
)
from services.core.jobs import EmitFn
from services.models import (
    DedupeMoveResponse,
    DedupeScanResult,
    DirsResponse,
    JobSnapshot,
    JobSubmitResponse,
)
from services.core.scanner import scan_images
from services.core.work_dir import list_work_dirs, resolve_subdirs

router = APIRouter(prefix="/dedupe", tags=["dedupe"])


class ScanBody(BaseModel):
    """去重扫描请求. 引用: ``POST /dedupe/scan``. """

    dirs: list[str] = Field(default_factory=lambda: ["main"])
    dir: str | None = None
    algorithm: str = Field(default="mse", description="mse | fft | orb")
    mse_threshold: float = Field(default=1e-4, gt=0, le=1)
    sim_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    similar_res_ratio: float = Field(default=1.05, ge=1.0, le=10.0)


class ApplyBody(BaseModel):
    """去重删除请求. 引用: ``POST /dedupe/apply``. """

    paths: list[str]
    dirs: list[str] = Field(default_factory=lambda: ["main"])
    dir: str | None = None


def _names(dirs: list[str] | None, legacy: str | None) -> list[str]:
    """解析目录名列表. 引用: ``scan_duplicates`` / ``apply_dedupe_delete``. """
    if dirs:
        return list(dirs)
    if legacy:
        return [legacy]
    return ["main"]


def similarity_histogram(values, algorithm: str) -> str | None:
    """两两相似度直方图, 以 base64 data URL 返回(MSE 用对数横轴).

    依赖 matplotlib (Agg 后端). matplotlib 缺失或无可绘制数据时返回 None.

    Args:
        values: 两两相似度值数组.
        algorithm: 相似度算法, ``"mse"`` / ``"fft"`` / ``"orb"``.

    Returns:
        str | None: ``data:image/png;base64,...``, 失败时 None.

    Callers:
        - `routes/dedupe.py:scan_duplicates`
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
def dedupe_dirs(request: Request) -> DirsResponse:
    """列出可扫描工作目录. 引用: ``dedupe.js``. """
    rt = get_runtime(request)
    return DirsResponse(dirs=list_work_dirs(rt.root), default="main")


@router.post("/scan")
async def scan_duplicates(request: Request, body: ScanBody) -> JobSubmitResponse:
    """
    提交去重扫描长任务. 

    返回 job_id；结束事件的 result 字段含分组结果. 
    引用: dedupe.js. 
    """
    rt = get_runtime(request)
    root = rt.root
    thumbnailer = rt.thumbnailer
    names = _names(body.dirs, body.dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    mse = body.mse_threshold
    ratio = body.similar_res_ratio
    algorithm = "mse" if body.algorithm not in ("mse", "fft", "orb") else body.algorithm
    sim = body.sim_threshold

    def run(emit: EmitFn) -> DedupeScanResult:
        """工作线程: 扫描重复组. 引用: ``JobManager.submit``. """
        emit(
            JobSnapshot(
                status="running",
                phase="scanning",
                message="扫描中…",
                pct=0,
            )
        )
        records = []
        for sub, work in pairs:
            part = load_records(work)
            for r in part:
                r.rel = f"{sub}/{r.rel}" if not r.rel.startswith(sub + "/") else r.rel
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
                """特征提取逐张进度上报. 引用: ``extract_*_features``. """
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
        groups = find_duplicate_groups(
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

        histogram = similarity_histogram(pairwise_values(records, algorithm), algorithm)

        result = DedupeScanResult(
            dirs=[s for s, _ in pairs],
            dir=pairs[0][0],
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

    job_id = await jobs(request).submit(kind="dedupe_scan", group="compute", dataset=rt.name, run=run)
    return JobSubmitResponse(ok=True, job_id=job_id)


@router.post("/apply")
def apply_dedupe_delete(request: Request, body: ApplyBody) -> DedupeMoveResponse:
    """将手选路径移入回收站(与筛选页同一套 trash). """
    rt = get_runtime(request)
    root = rt.root
    state_mgr = rt.state_manager
    if not body.paths:
        raise HTTPException(400, "未选择任何图片")

    names = _names(body.dirs, body.dir)
    try:
        pairs = resolve_subdirs(root, names)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    alive: set[str] = set()
    for sub, work in pairs:
        alive |= {img.path for img in scan_images(work, path_prefix=sub)}

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
    idx = rt.color_index
    if idx is not None:
        idx.clear()

    return DedupeMoveResponse(
        moved=moved,
        paths=paths,
        dirs=[s for s, _ in pairs],
    )