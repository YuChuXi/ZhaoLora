"""WD14 批量打标。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_THRESHOLD = 0.1

_REPO_ROOT = Path(__file__).resolve().parents[1].parent
WD14_MODEL_DIR = _REPO_ROOT / "wd14_tagger_model" / "SmilingWolf_wd-vit-large-tagger-v3"
WD14_ONNX_PATH = WD14_MODEL_DIR / "model.onnx"
WD14_CSV_PATH = WD14_MODEL_DIR / "selected_tags.csv"

_WD14_SESSION = None
_WD14_LABELS: list[str] = []
_WD14_LOCK = threading.Lock()


class Tags:
    @staticmethod
    def split_caption(caption: str) -> list[str]:
        return [x.strip() for x in caption.replace("\n", ",").split(",") if x.strip()]

    @staticmethod
    def join_caption(tags: Iterable[str]) -> str:
        return ", ".join([t.strip() for t in tags if t.strip()])

    @staticmethod
    def equivalence_key(tag: str) -> str:
        return " ".join(tag.strip().lower().replace("_", " ").split())

    @staticmethod
    def _canonical_spacing(tag: str) -> str:
        return " ".join(tag.strip().replace("_", " ").split())

    @staticmethod
    def merge_for_disk_by_confidence(base: list[str], wd_scored: list[tuple[str, float]]) -> str:
        score_by_key: dict[str, float] = {}
        canonical_by_key: dict[str, str] = {}
        for tag, score in wd_scored:
            key = Tags.equivalence_key(tag)
            if key not in score_by_key or score > score_by_key[key]:
                score_by_key[key] = score
                canonical_by_key[key] = Tags._canonical_spacing(tag)
        for raw in base:
            key = Tags.equivalence_key(raw)
            if key not in canonical_by_key:
                canonical_by_key[key] = Tags._canonical_spacing(raw)
                score_by_key[key] = -1.0
        ordered = sorted(score_by_key.keys(), key=lambda k: score_by_key[k], reverse=True)
        return Tags.join_caption([canonical_by_key[k] for k in ordered])


# 打标页不列出这些目录（mask 是 alpha，非待打标图）
_SKIP_TAG_DIRS = frozenset({"mask", ".trash", ".thumbs", "__pycache__"})


def list_image_subdirs(root: Path) -> list[dict]:
    """列出根下含图片的一级子目录。"""
    root = root.resolve()
    out: list[dict] = []
    if not root.is_dir():
        return out

    def count_images(d: Path) -> int:
        return sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)

    n_root = count_images(root)
    if n_root:
        out.append({"name": ".", "label": "(根目录)", "image_count": n_root})

    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.startswith(".") or p.name in _SKIP_TAG_DIRS:
            continue
        n = count_images(p)
        if n > 0:
            out.append({"name": p.name, "label": p.name, "image_count": n})
    return out


def resolve_image_dirs(root: Path, names: list[str] | None) -> list[Path]:
    root = root.resolve()
    available = {
        d["name"]: root if d["name"] == "." else root / d["name"]
        for d in list_image_subdirs(root)
    }
    if not names:
        return []
    dirs: list[Path] = []
    for name in names:
        name = (name or "").strip()
        if name in available:
            dirs.append(available[name])
    return dirs


def model_ready() -> tuple[bool, str]:
    if WD14_ONNX_PATH.is_file() and WD14_CSV_PATH.is_file():
        return True, str(WD14_MODEL_DIR)
    return False, f"找不到本地 WD14 模型: {WD14_MODEL_DIR}"


def _get_wd14():
    global _WD14_SESSION, _WD14_LABELS
    with _WD14_LOCK:
        if _WD14_SESSION is not None:
            return _WD14_SESSION, _WD14_LABELS
        ok, msg = model_ready()
        if not ok:
            raise RuntimeError(msg)
        import onnxruntime as ort
        import pandas as pd
        import torch

        df = pd.read_csv(WD14_CSV_PATH)
        _WD14_LABELS = df["name"].tolist()
        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if torch.cuda.is_available()
            else ["CPUExecutionProvider"]
        )
        _WD14_SESSION = ort.InferenceSession(str(WD14_ONNX_PATH), providers=providers)
        return _WD14_SESSION, _WD14_LABELS


def run_wd14_on_image_scored(img: Image.Image, threshold: float) -> list[tuple[str, float]]:
    session, labels = _get_wd14()
    if img.mode == "RGBA":
        rgb = Image.new("RGB", img.size, (255, 255, 255))
        rgb.paste(img, mask=img.split()[3])
        img = rgb
    else:
        img = img.convert("RGB")
    w, h = img.size
    max_side = max(w, h)
    canvas = Image.new("RGB", (max_side, max_side), (255, 255, 255))
    canvas.paste(img, ((max_side - w) // 2, (max_side - h) // 2))
    input_img = canvas.resize((448, 448), Image.Resampling.LANCZOS)
    arr = np.array(input_img).astype(np.float32)
    arr = arr[:, :, ::-1]
    input_data = arr[np.newaxis, ...]
    input_name = session.get_inputs()[0].name
    probs = session.run(None, {input_name: input_data})[0][0]
    scored: list[tuple[str, float]] = []
    for i, p in enumerate(probs):
        if i < len(labels) and float(p) >= threshold:
            name = labels[i]
            if name in ["general", "sensitive", "questionable", "explicit"]:
                continue
            scored.append((name.replace("_", " ").strip(), float(p)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _load_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


@dataclass
class TaggingProgress:
    status: str = "idle"  # idle|running|done|error|cancelled
    phase: str = "idle"  # idle|loading|running|done
    total: int = 0
    done: int = 0
    skipped: int = 0
    ok: int = 0
    failed: int = 0
    current: str = ""
    message: str = ""
    logs: list[str] = field(default_factory=list)
    cancel_requested: bool = False

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
            "skipped": self.skipped,
            "ok": self.ok,
            "failed": self.failed,
            "current": self.current,
            "message": self.message,
            "logs": list(self.logs[-80:]),
            "cancel_requested": self.cancel_requested,
        }

    def log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-300:]


def collect_jobs(dirs: list[Path]) -> list[tuple[Path, Path]]:
    """返回 (src_image, out_dir) 列表。"""
    items: list[tuple[Path, Path]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                items.append((p, d))
    return items


def process_one(
    src: Path,
    *,
    threshold: float,
    disable_orig_tags: bool,
    force: bool,
) -> str:
    """处理单张：只写同名 .txt。返回 skip|ok。"""
    out_txt_path = src.with_suffix(".txt")
    if not force and out_txt_path.exists():
        return "skip"

    img = Image.open(src)
    base: list[str] = []
    if not disable_orig_tags:
        base = Tags.split_caption(_load_text(out_txt_path))
    wd_scored = run_wd14_on_image_scored(img, threshold)
    caption = Tags.merge_for_disk_by_confidence(base, wd_scored)
    out_txt_path.write_text(caption, encoding="utf-8")
    return "ok"


def run_batch(
    dirs: list[Path],
    *,
    threshold: float,
    disable_orig_tags: bool,
    force: bool,
    progress: TaggingProgress,
    on_update: Callable[[], None] | None = None,
) -> None:
    items = collect_jobs(dirs)
    progress.total = len(items)
    progress.done = 0
    progress.skipped = 0
    progress.ok = 0
    progress.failed = 0
    progress.status = "running"
    progress.phase = "loading"
    progress.message = f"共 {len(items)} 张，加载模型…"
    progress.log(
        f"开始: dirs={[d.name for d in dirs]}, threshold={threshold}, "
        f"disable_orig_tags={disable_orig_tags}, force={force}, total={len(items)}"
    )
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

    progress.log("加载 WD14 模型…")
    _get_wd14()
    progress.phase = "running"
    progress.message = f"共 {len(items)} 张"
    if on_update:
        on_update()

    for i, (src, out_dir) in enumerate(items):
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.message = f"已取消 ({progress.ok} 成功 / {progress.skipped} 跳过)"
            progress.log(progress.message)
            if on_update:
                on_update()
            return

        rel = f"{out_dir.name}/{src.name}"
        progress.current = rel
        progress.message = f"[{i + 1}/{len(items)}] {rel}"
        if on_update:
            on_update()

        try:
            result = process_one(
                src,
                threshold=threshold,
                disable_orig_tags=disable_orig_tags,
                force=force,
            )
        except (OSError, ValueError, RuntimeError) as e:
            progress.failed += 1
            progress.done += 1
            progress.log(f"[{i + 1}/{len(items)}] 失败: {rel} — {e}")
            if on_update:
                on_update()
            continue
        if result == "skip":
            progress.skipped += 1
            progress.log(f"[{i + 1}/{len(items)}] 跳过(已存在): {rel}")
        else:
            progress.ok += 1
            progress.log(f"[{i + 1}/{len(items)}] 完成: {rel}")
        progress.done += 1
        if on_update:
            on_update()

    progress.status = "done"
    progress.phase = "done"
    progress.current = ""
    progress.message = (
        f"完成: 新处理 {progress.ok} 张, 跳过 {progress.skipped} 张, "
        f"失败 {progress.failed} 张, 合计 {progress.total} 张"
    )
    progress.log(progress.message)
    if on_update:
        on_update()
