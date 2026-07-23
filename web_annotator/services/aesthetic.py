"""美学评分：CLIP ViT-L/14 + purplesmartai aesthetic-classifier。"""

from __future__ import annotations

import importlib.util
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import torch
from PIL import Image, UnidentifiedImageError

from services.wd14_batch import IMAGE_EXTS, list_image_subdirs, resolve_image_dirs

_REPO_ROOT = Path(__file__).resolve().parents[1].parent
_SCRIPT = _REPO_ROOT / "aesthetic_score_dataset_pony.py"

_MOD = None
_MODELS: dict[str, tuple] = {}
_MODEL_LOCK = threading.Lock()


def _pony():
    global _MOD
    if _MOD is not None:
        return _MOD
    if not _SCRIPT.is_file():
        raise RuntimeError(f"找不到脚本: {_SCRIPT}")
    spec = importlib.util.spec_from_file_location("aesthetic_score_dataset_pony", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MOD = mod
    return _MOD


def cuda_available() -> bool:
    return bool(torch.cuda.is_available())


def default_device() -> str:
    return "cuda" if cuda_available() else "cpu"


def get_models(device: str):
    device = "cuda" if device == "cuda" and cuda_available() else "cpu"
    with _MODEL_LOCK:
        cached = _MODELS.get(device)
        if cached is not None:
            return cached
        clip_model, preprocess, aesthetic_model = _pony().load_models(device)
        _MODELS[device] = (clip_model, preprocess, aesthetic_model, device)
        return _MODELS[device]


@dataclass
class AestheticProgress:
    status: str = "idle"
    phase: str = "idle"  # idle|loading|running|done
    total: int = 0
    done: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    renamed: int = 0
    current: str = ""
    message: str = ""
    logs: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    summary: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        total = self.total
        done = self.done
        pct = round(100.0 * done / total, 1) if total > 0 else 0.0
        if self.status == "done" and total > 0:
            pct = 100.0
        return {
            "status": self.status,
            "phase": self.phase,
            "total": total,
            "done": done,
            "pct": pct,
            "ok": self.ok,
            "skipped": self.skipped,
            "failed": self.failed,
            "renamed": self.renamed,
            "current": self.current,
            "message": self.message,
            "logs": list(self.logs[-80:]),
            "cancel_requested": self.cancel_requested,
            "summary": dict(self.summary),
        }

    def log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-300:]


def _collect(dirs: list[Path]) -> list[tuple[Path, Path]]:
    items: list[tuple[Path, Path]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                items.append((p, d))
    return items


def _rename_one(img_path: Path, raw: float) -> Path:
    """按 {raw:.2f}_原名 重命名图片及同名 .txt；已有前缀则跳过。"""
    old_stem = img_path.stem
    if re.match(r"^\d+\.\d{2}_", old_stem):
        return img_path
    new_name = f"{raw:.2f}_{old_stem}{img_path.suffix}"
    new_path = img_path.with_name(new_name)
    img_path.rename(new_path)
    txt_path = img_path.with_suffix(".txt")
    if txt_path.is_file():
        txt_path.rename(new_path.with_suffix(".txt"))
    return new_path


def run_aesthetic(
    dirs: list[Path],
    *,
    root: Path,
    device: str,
    rename: bool,
    write_report: bool,
    progress: AestheticProgress,
) -> None:
    root = root.resolve()
    items = _collect(dirs)
    progress.total = len(items)
    progress.done = 0
    progress.ok = 0
    progress.skipped = 0
    progress.failed = 0
    progress.renamed = 0
    progress.summary = {}
    progress.status = "running"
    progress.phase = "loading"
    progress.message = f"共 {len(items)} 张，加载模型…"
    progress.log(
        f"开始: dirs={[d.name for d in dirs]}, device={device}, rename={rename}, total={len(items)}"
    )

    if not items:
        progress.status = "done"
        progress.phase = "done"
        progress.message = "没有可处理的图片"
        progress.log(progress.message)
        return

    progress.log("加载模型…")
    clip_model, preprocess, aesthetic_model, device = get_models(device)
    mod = _pony()
    progress.phase = "running"
    progress.message = f"共 {len(items)} 张"
    progress.log(f"模型就绪 ({device})")

    results: list[dict] = []

    for i, (src, parent) in enumerate(items):
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.message = f"已取消 ({progress.ok} 完成)"
            progress.log(progress.message)
            return

        rel = f"{parent.name}/{src.name}" if parent != root else src.name
        progress.current = rel
        progress.message = f"[{i + 1}/{len(items)}] {rel}"

        try:
            img = Image.open(src)
            raw = float(mod.get_score(img, clip_model, preprocess, aesthetic_model, device))
            pony = int(mod.raw_to_pony(raw))
            cur = src
            if rename:
                new_path = _rename_one(src, raw)
                if new_path != src:
                    progress.renamed += 1
                    cur = new_path
            rel_to_root = str(cur.resolve().relative_to(root))
            results.append({
                "path": str(cur),
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
            continue

    if write_report and results:
        html_path = root / "aesthetic_report.html"
        mod.generate_html(results, root.name, str(html_path), str(root))
        progress.log(f"报告: {html_path}")
        progress.summary["report"] = str(html_path)

    scores = [r["raw"] for r in results]
    pony_counts = [0] * 10
    for r in results:
        pony_counts[r["pony"]] += 1

    mean_v = float(np.mean(scores)) if scores else 0.0
    progress.summary = {
        **progress.summary,
        "count": len(results),
        "mean": round(mean_v, 4),
        "median": round(float(np.median(scores)), 4) if scores else 0.0,
        "std": round(float(np.std(scores)), 4) if scores else 0.0,
        "min": round(float(np.min(scores)), 4) if scores else 0.0,
        "max": round(float(np.max(scores)), 4) if scores else 0.0,
        "pony_counts": pony_counts,
        "renamed": progress.renamed,
    }
    progress.status = "done"
    progress.phase = "done"
    progress.current = ""
    progress.message = (
        f"完成 {progress.ok} 张"
        + (f"，重命名 {progress.renamed}" if rename else "")
        + f"，均值 {progress.summary['mean']}"
    )
    progress.log(progress.message)


# 供路由复用
__all__ = [
    "AestheticProgress",
    "cuda_available",
    "default_device",
    "list_image_subdirs",
    "resolve_image_dirs",
    "run_aesthetic",
]
