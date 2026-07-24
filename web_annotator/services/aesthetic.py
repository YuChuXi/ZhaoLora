"""美学评分：CLIP ViT-L/14 + purplesmartai aesthetic-classifier。"""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import numpy as np
import torch
import torch.nn as nn
from PIL import Image, UnidentifiedImageError

from services.models import AestheticSummary, JobSnapshot
from services.wd14_batch import IMAGE_EXTS, list_image_subdirs, resolve_image_dirs

_REPO_ROOT = Path(__file__).resolve().parents[1].parent
_SCRIPT = _REPO_ROOT / "aesthetic_score_dataset_pony.py"
SCORES_NAME = ".aesthetic_scores.json"

_MOD: ModuleType | None = None
_MODELS: dict[str, tuple[nn.Module, Callable[[Image.Image], torch.Tensor], nn.Module, str]] = {}
_MODEL_LOCK = threading.Lock()


def _pony() -> ModuleType:
    """加载美学评分脚本模块。引用：``get_models`` / ``run_aesthetic``。"""
    global _MOD
    if _MOD is not None:
        return _MOD
    if not _SCRIPT.is_file():
        raise RuntimeError(f"找不到脚本: {_SCRIPT}")
    spec = importlib.util.spec_from_file_location("aesthetic_score_dataset_pony", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    _MOD = mod
    return _MOD


def cuda_available() -> bool:
    """当前环境是否有 CUDA。引用：``routes.aesthetic``。"""
    return bool(torch.cuda.is_available())


def default_device() -> str:
    """默认推理设备。引用：``routes.aesthetic.aesthetic_dirs``。"""
    return "cuda" if cuda_available() else "cpu"


def get_models(device: str) -> tuple[nn.Module, Callable[[Image.Image], torch.Tensor], nn.Module, str]:
    """加载或复用 CLIP / 美学头。引用：``run_aesthetic``。"""
    device = "cuda" if device == "cuda" and cuda_available() else "cpu"
    with _MODEL_LOCK:
        cached = _MODELS.get(device)
        if cached is not None:
            return cached
        clip_model, preprocess, aesthetic_model = _pony().load_models(device)
        packed: tuple[nn.Module, Callable[[Image.Image], torch.Tensor], nn.Module, str] = (
            clip_model,
            preprocess,
            aesthetic_model,
            device,
        )
        _MODELS[device] = packed
        return packed


@dataclass
class AestheticProgress:
    status: str = "idle"
    phase: str = "idle"  # idle|loading|running|done
    total: int = 0
    done: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    current: str = ""
    message: str = ""
    logs: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    summary: AestheticSummary = field(default_factory=AestheticSummary)

    def snapshot(self) -> JobSnapshot:
        """导出进度快照。引用：``routes.aesthetic`` 长任务 emit。"""
        total = self.total
        done = self.done
        pct = round(100.0 * done / total, 1) if total > 0 else 0.0
        if self.status == "done" and total > 0:
            pct = 100.0
        return JobSnapshot(
            status=self.status,
            phase=self.phase,
            total=total,
            done=done,
            pct=pct,
            ok=self.ok,
            skipped=self.skipped,
            failed=self.failed,
            current=self.current,
            message=self.message,
            logs=list(self.logs[-80:]),
            cancel_requested=self.cancel_requested,
            summary=self.summary.model_copy(),
        )

    def log(self, line: str) -> None:
        """追加美学任务日志。引用：run_aesthetic。"""
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-300:]


def scores_path(root: Path) -> Path:
    """美学分 JSON 路径。引用：load_scores / save_scores。"""
    return root.resolve() / SCORES_NAME


def load_scores(root: Path) -> dict[str, float]:
    """读取美学分数表。

    Raises:
        json.JSONDecodeError: 文件不是合法 JSON。
        TypeError: 根节点不是对象。
        ValueError: 分值无法转为 float。

    引用:
        ``routes.images``；``run_aesthetic``。
    """
    path = scores_path(root)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"美学分数文件根节点必须是对象: {path}")
    out: dict[str, float] = {}
    for k, v in data.items():
        key = str(k).replace("\\", "/").lstrip("/")
        out[key] = float(v)
    return out


def save_scores(root: Path, scores: dict[str, float]) -> None:
    """写入相对路径→分数表。引用：run_aesthetic。"""
    path = scores_path(root)
    clean = {
        str(k).replace("\\", "/").lstrip("/"): round(float(v), 6)
        for k, v in scores.items()
    }
    path.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _collect(dirs: list[Path]) -> list[tuple[Path, Path]]:
    """收集待评分 (图路径, 目录)。引用：run_aesthetic。"""
    items: list[tuple[Path, Path]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                items.append((p, d))
    return items


def run_aesthetic(
    dirs: list[Path],
    *,
    root: Path,
    device: str,
    write_report: bool,
    progress: AestheticProgress,
    on_update: Callable[[], None] | None = None,
) -> None:
    """
    对所选目录跑美学评分。

    Args:
        on_update: 进度字段变更后的同步回调；由 jobs 桥接到事件循环。
            引用：routes/aesthetic.py 提交任务时传入。
    """
    root = root.resolve()
    items = _collect(dirs)
    progress.total = len(items)
    progress.done = 0
    progress.ok = 0
    progress.skipped = 0
    progress.failed = 0
    progress.summary = AestheticSummary()
    progress.status = "running"
    progress.phase = "loading"
    progress.message = f"共 {len(items)} 张，加载模型…"
    progress.log(f"开始: dirs={[d.name for d in dirs]}, device={device}, total={len(items)}")
    if on_update:
        on_update()

    if not items:
        progress.status = "done"
        progress.phase = "done"
        progress.message = "没有可处理的图片"
        progress.log(progress.message)
        if on_update:
            on_update()
        return

    progress.log("加载模型…")
    clip_model, preprocess, aesthetic_model, device = get_models(device)
    mod = _pony()
    progress.phase = "running"
    progress.message = f"共 {len(items)} 张"
    progress.log(f"模型就绪 ({device})")
    if on_update:
        on_update()

    results: list[dict] = []
    score_map = load_scores(root)

    for i, (src, parent) in enumerate(items):
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.message = f"已取消 ({progress.ok} 完成)"
            progress.log(progress.message)
            if results:
                save_scores(root, score_map)
            if on_update:
                on_update()
            return

        rel = f"{parent.name}/{src.name}" if parent != root else src.name
        progress.current = rel
        progress.message = f"[{i + 1}/{len(items)}] {rel}"
        if on_update:
            on_update()

        try:
            img = Image.open(src)
            raw = float(mod.get_score(img, clip_model, preprocess, aesthetic_model, device))
            pony = int(mod.raw_to_pony(raw))
            rel_to_root = str(src.resolve().relative_to(root)).replace("\\", "/")
            score_map[rel_to_root] = round(raw, 6)
            results.append({
                "path": str(src),
                "rel_path": rel_to_root,
                "raw": round(raw, 6),
                "pony": pony,
            })
            progress.ok += 1
            progress.done += 1
            progress.log(f"[{i + 1}/{len(items)}] score_{pony} {raw:.4f}  {rel_to_root}")
        except (OSError, UnidentifiedImageError, RuntimeError, ValueError) as e:
            progress.failed += 1
            progress.done += 1
            progress.log(f"[{i + 1}/{len(items)}] 失败: {rel} — {e}")
        if on_update:
            on_update()

    save_scores(root, score_map)
    progress.log(f"分数: {scores_path(root)}")

    if write_report and results:
        html_path = root / "aesthetic_report.html"
        mod.generate_html(results, root.name, str(html_path), str(root))
        progress.log(f"报告: {html_path}")
        progress.summary.report = str(html_path)

    scores = [r["raw"] for r in results]
    pony_counts = [0] * 10
    for r in results:
        pony_counts[r["pony"]] += 1

    mean_v = float(np.mean(scores)) if scores else 0.0
    progress.summary = progress.summary.model_copy(
        update={
            "count": len(results),
            "mean": round(mean_v, 4),
            "median": round(float(np.median(scores)), 4) if scores else 0.0,
            "std": round(float(np.std(scores)), 4) if scores else 0.0,
            "min": round(float(np.min(scores)), 4) if scores else 0.0,
            "max": round(float(np.max(scores)), 4) if scores else 0.0,
            "pony_counts": pony_counts,
            "scores_file": str(scores_path(root)),
        }
    )
    progress.status = "done"
    progress.phase = "done"
    progress.current = ""
    progress.message = f"完成 {progress.ok} 张，均值 {progress.summary.mean}"
    progress.log(progress.message)
    if on_update:
        on_update()


__all__ = [
    "AestheticProgress",
    "SCORES_NAME",
    "cuda_available",
    "default_device",
    "list_image_subdirs",
    "load_scores",
    "resolve_image_dirs",
    "run_aesthetic",
    "save_scores",
    "scores_path",
]
