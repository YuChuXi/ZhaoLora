"""WD14 批量打标. """

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

import numpy as np
from PIL import Image

from services.models import JobSnapshot, WorkDirInfo
from services.core.scanner import IMAGE_EXTS, collect_images

if TYPE_CHECKING:
    from onnxruntime import InferenceSession

DEFAULT_THRESHOLD = 0.1

_REPO_ROOT = Path(__file__).resolve().parents[3]
WD14_MODEL_DIR = _REPO_ROOT / "wd14_tagger_model" / "SmilingWolf_wd-vit-large-tagger-v3"
WD14_ONNX_PATH = WD14_MODEL_DIR / "model.onnx"
WD14_CSV_PATH = WD14_MODEL_DIR / "selected_tags.csv"

_WD14_SESSION: InferenceSession | None = None
_WD14_LABELS: list[str] = []
_WD14_LOCK = threading.Lock()


class Tags:
    """WD14 标签工具集, 提供拆分、拼接、等价键比较与置信度合并. 

    Callers:
        - ``process_one``: 调用 ``split_caption``、``merge_for_disk_by_confidence``
    """

    @staticmethod
    def split_caption(caption: str) -> list[str]:
        """按逗号拆分 caption. 

        Callers:
            - ``Tags.merge_for_disk_by_confidence``
            - ``process_one``
        """
        return [x.strip() for x in caption.replace("\n", ",").split(",") if x.strip()]

    @staticmethod
    def join_caption(tags: Iterable[str]) -> str:
        """用逗号空格拼接标签. 

        Callers:
            - ``Tags.merge_for_disk_by_confidence``
        """
        return ", ".join([t.strip() for t in tags if t.strip()])

    @staticmethod
    def equivalence_key(tag: str) -> str:
        """标签等价键(小写、空格化). 

        Callers:
            - ``Tags.merge_for_disk_by_confidence``
        """
        return " ".join(tag.strip().lower().replace("_", " ").split())

    @staticmethod
    def _canonical_spacing(tag: str) -> str:
        """规范化空格, 保留大小写. 

        Callers:
            - ``Tags.merge_for_disk_by_confidence``
        """
        return " ".join(tag.strip().replace("_", " ").split())

    @staticmethod
    def merge_for_disk_by_confidence(base: list[str], wd_scored: list[tuple[str, float]]) -> str:
        """按置信度合并原标签与 WD14 结果. 

        Callers:
            - ``process_one``
        """
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


# 打标页不列出这些目录(mask 是 alpha, 非待打标图)
_SKIP_TAG_DIRS = frozenset({
    "mask",
    "aesthetic_report",
    ".trash",
    ".thumbs",
    ".thumbnails",
    "__pycache__",
})


def list_image_subdirs(root: Path) -> list[WorkDirInfo]:
    """列出根下含图片的一级子目录. 

    引用:
        ``services/core/work_dir.py:list_work_dirs``；``resolve_image_dirs``；打标 / 美学目录选择. 
    """
    root = root.resolve()
    out: list[WorkDirInfo] = []
    if not root.is_dir():
        return out

    def count_images(d: Path) -> int:
        """统计目录内图像数. 引用: ``list_image_subdirs``. """
        return sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)

    n_root = count_images(root)
    if n_root:
        out.append(WorkDirInfo(name=".", label="(根目录)", image_count=n_root))

    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.startswith(".") or p.name in _SKIP_TAG_DIRS:
            continue
        n = count_images(p)
        if n > 0 or p.name == "main":
            out.append(WorkDirInfo(name=p.name, label=p.name, image_count=n))

    root_item = [d for d in out if d.name == "."]
    rest = [d for d in out if d.name != "."]
    main = [d for d in rest if d.name == "main"]
    others = [d for d in rest if d.name != "main"]
    if not main and (root / "main").is_dir():
        main = [WorkDirInfo(name="main", label="main", image_count=count_images(root / "main"))]
    return root_item + main + others


def resolve_image_dirs(root: Path, names: list[str] | None) -> list[Path]:
    """将子目录名解析为绝对路径；未知名跳过. 

    Callers:
        - ``routes/scoring.py``
        - ``routes/tagging.py``
    """
    root = root.resolve()
    available = {
        d.name: root if d.name == "." else root / d.name
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
    """检查本地 WD14 权重是否齐全; 返回相对仓库根的模型目录路径. 

    Callers:
        - ``routes/tagging.py``
    """
    rel = WD14_MODEL_DIR.relative_to(_REPO_ROOT).as_posix()
    if WD14_ONNX_PATH.is_file() and WD14_CSV_PATH.is_file():
        return True, rel
    return False, f"找不到本地 WD14 模型: {rel}"


def _get_wd14() -> tuple[InferenceSession, list[str]]:
    """加载或复用 WD14 ONNX 会话. 引用: ``run_wd14_on_image_scored``. """
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
    """对单张图跑 WD14, 返回 (tag, score). 引用: process_one. """
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
    # 模型输出声明为稀疏张量, 类型推断为 SparseTensor, 但运行时返回 numpy 数组; 显式转 ndarray.
    probs = np.asarray(session.run(None, {input_name: input_data})[0])[0]
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
    """读取旁路 txt；不存在则空串. 

    Callers:
        - ``process_one``
    """
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

    def snapshot(self) -> JobSnapshot:
        """导出进度快照. 引用: ``routes.tagging`` 长任务 emit. """
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
            skipped=self.skipped,
            ok=self.ok,
            failed=self.failed,
            current=self.current,
            message=self.message,
            logs=list(self.logs[-80:]),
            cancel_requested=self.cancel_requested,
        )

    def log(self, line: str) -> None:
        """追加进度日志并截断. 引用: run_batch. """
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-300:]


def process_one(
    src: Path,
    *,
    threshold: float,
    disable_orig_tags: bool,
    force: bool,
) -> str:
    """处理单张图片: WD14 推理 → 合并原标签 → 写入同名 .txt. 

    Returns:
        ``"skip"`` 或 ``"ok"``. 

    Callers:
        - ``run_batch``
    """
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
    """批量打标主循环. 引用: ``routes/tagging.py``. """
    items = collect_images(dirs)
    progress.total = len(items)
    progress.done = 0
    progress.skipped = 0
    progress.ok = 0
    progress.failed = 0
    progress.status = "running"
    progress.phase = "loading"
    progress.message = f"共 {len(items)} 张, 加载模型…"
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

    for i, src in enumerate(items):
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.message = f"已取消 ({progress.ok} 成功 / {progress.skipped} 跳过)"
            progress.log(progress.message)
            if on_update:
                on_update()
            return

        rel = f"{src.parent.name}/{src.name}"
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
