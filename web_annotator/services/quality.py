"""质量评分：Q-ReAlign-Lite-4B 图像质量评估。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import torch
from PIL import Image, UnidentifiedImageError

from services.models import JobSnapshot
from services.scanner import IMAGE_EXTS

CKPT = "q-future/Q-ReAlign-Lite-4B"
LEVELS = ["excellent", "good", "fair", "poor", "bad"]
WEIGHTS = [1.0, 0.75, 0.5, 0.25, 0.0]

SCORES_NAME = ".quality_scores.json"
REPORT_NAME = "quality_report.html"
QUALITY_TAGS = ["masterpiece", "best quality", "good quality", "normal quality", "low quality", "worst quality"]

_MODEL_CACHE: tuple | None = None


def cuda_available() -> bool:
    """当前环境是否有 CUDA。

    Callers:
        - `routes/quality.py:quality_dirs`
    """
    return bool(torch.cuda.is_available())


def default_device() -> str:
    """默认推理设备。

    Callers:
        - `routes/quality.py:quality_dirs`
    """
    return "cuda" if cuda_available() else "cpu"


def _load_model(device: str) -> tuple:
    """加载 Q-ReAlign-Lite-4B 模型与处理器，按设备缓存。

    Callers:
        - `quality.py:run_quality`
    """
    global _MODEL_CACHE
    if _MODEL_CACHE is not None and _MODEL_CACHE[0] == device:
        return _MODEL_CACHE[1], _MODEL_CACHE[2]

    try:
        from transformers import AutoModelForImageTextToText, AutoProcessor
    except ImportError:
        raise RuntimeError("需要 transformers >= 5.2.0: pip install transformers huggingface_hub")

    processor = AutoProcessor.from_pretrained(CKPT)
    model = AutoModelForImageTextToText.from_pretrained(CKPT, dtype="auto").to(device).eval()
    _MODEL_CACHE = (device, model, processor)
    return model, processor


def _predict_quality(image: Image.Image, model, processor, device: str) -> float:
    """对单张图片预测质量分 [0, 1]。

    Callers:
        - `quality.py:run_quality`
    """
    messages = [{"role": "user", "content": [
        {"type": "image"},
        {"type": "text", "text": "How would you rate the quality of this image?"},
    ]}]
    text = processor.apply_chat_template(messages, add_generation_prompt=True) + "The quality of the image is"
    inputs = processor(
        text=[text],
        images=[image.convert("RGB")],
        return_tensors="pt",
    ).to(device)

    ids = [processor.tokenizer(" " + w, add_special_tokens=False).input_ids[0] for w in LEVELS]
    with torch.no_grad():
        probs = model(**inputs).logits[0, -1, ids].softmax(-1)
    score = (probs * torch.tensor(WEIGHTS, device=device)).sum().item()
    return float(score)


def quality_to_tag(score: float) -> str:
    """质量分 → 质量标签。

    映射规则（等宽分箱）：
        [0.833, 1.00] → masterpiece
        [0.667, 0.833) → best quality
        [0.500, 0.667) → good quality
        [0.333, 0.500) → normal quality
        [0.167, 0.333) → low quality
        [0.000, 0.167) → worst quality

    Callers:
        - `quality.py:run_quality`
        - `tag_clean.py:clean_txt_dirs`
    """
    i = min(int(score * len(QUALITY_TAGS)), len(QUALITY_TAGS) - 1)
    return QUALITY_TAGS[max(0, i)]


def scores_path(root: Path) -> Path:
    """质量分 JSON 路径。

    Callers:
        - `quality.py:load_quality_scores`
        - `quality.py:save_quality_scores`
    """
    return root.resolve() / SCORES_NAME


def load_quality_scores(root: Path) -> dict[str, float]:
    """读取质量分数表。

    Returns:
        相对路径 → [0, 1] 质量分。

    Callers:
        - `routes.images`
        - `routes.tag_stats`
    """
    path = scores_path(root)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"质量分数文件根节点必须是对象: {path}")
    out: dict[str, float] = {}
    for k, v in data.items():
        key = str(k).replace("\\", "/").lstrip("/")
        out[key] = float(v)
    return out


def save_quality_scores(root: Path, scores: dict[str, float]) -> None:
    """写入质量分 JSON。

    Callers:
        - `quality.py:run_quality`
    """
    path = scores_path(root)
    clean = {
        str(k).replace("\\", "/").lstrip("/"): round(float(v), 6)
        for k, v in scores.items()
    }
    path.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def report_path(root: Path) -> Path:
    """质量报告 HTML 路径。

    Callers:
        - `routes.quality`
    """
    return root.resolve() / REPORT_NAME


@dataclass
class QualityProgress:
    """质量评分进度追踪。"""

    status: str = "idle"
    phase: str = "idle"
    total: int = 0
    done: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    current: str = ""
    message: str = ""
    logs: list[str] = field(default_factory=list)
    cancel_requested: bool = False

    def snapshot(self) -> JobSnapshot:
        """导出进度快照。

        Callers:
            - `routes.quality`
        """
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
        )

    def log(self, line: str) -> None:
        """追加日志。

        Callers:
            - `quality.py:run_quality`
        """
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-300:]


def _generate_html(
    results: list[dict[str, Any]],
    title: str,
    out_path: Path,
    image_root: Path,
) -> None:
    """生成质量评分 HTML 报告（紧凑版美学报告风格）。

    Callers:
        - `quality.py:run_quality`
    """
    image_root = image_root.resolve()
    cards: list[str] = []
    for r in results:
        score = r["quality"]
        tag = r["quality_tag"]
        path = r["path"]
        img_path = image_root / path
        rel = img_path.relative_to(out_path.parent, walk=True) if img_path.is_absolute() else path
        cards.append(f"""<div class="card">
  <div class="card-score">{score:.4f}</div>
  <div class="card-tag">[{tag}]</div>
  <img src="{rel}" loading="lazy" onclick="openLightbox('{rel}')">
  <div class="card-name">{Path(path).name}</div>
</div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — 质量评分报告</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #1a1a1a; color: #ddd; font-family: system-ui, sans-serif; }}
header {{ position: sticky; top: 0; z-index: 10; background: #222; border-bottom: 1px solid #444; padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; }}
header h1 {{ font-size: 16px; }}
.grid {{ display: flex; flex-wrap: wrap; gap: 8px; padding: 12px; }}
.card {{ background: #2a2a2a; border-radius: 6px; overflow: hidden; width: 200px; position: relative; }}
.card img {{ width: 100%; display: block; cursor: pointer; }}
.card-score {{ position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,0.7); color: #0f0; font-size: 11px; padding: 2px 6px; border-radius: 3px; font-weight: bold; }}
.card-tag {{ position: absolute; top: 4px; right: 4px; background: rgba(0,0,0,0.7); color: #ff0; font-size: 10px; padding: 2px 6px; border-radius: 3px; }}
.card-name {{ font-size: 11px; padding: 4px 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.lightbox {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.9); z-index: 999; align-items: center; justify-content: center; }}
.lightbox.active {{ display: flex; }}
.lightbox img {{ max-width: 95vw; max-height: 95vh; }}
</style>
</head>
<body>
<header>
  <h1>{title} — 质量评分报告</h1>
  <span>{len(results)} 张图片</span>
</header>
<div class="grid">
{chr(10).join(cards)}
</div>
<div class="lightbox" id="lb" onclick="this.classList.remove('active')">
  <img id="lb-img" src="">
</div>
<script>
function openLightbox(src) {{
  document.getElementById('lb-img').src = src;
  document.getElementById('lb').classList.add('active');
}}
</script>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def collect_image_dirs(root: Path) -> list[Path]:
    """扫描数据集根目录下所有包含图片的子目录。

    Callers:
        - `routes.quality`
    """
    dirs: list[Path] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name == "mask":
            continue
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                dirs.append(d)
                break
    return dirs


def _collect(root: Path, dirs: list[Path]) -> list[tuple[Path, Path]]:
    """收集待评分 (图路径, 父目录)。

    Callers:
        - `quality.py:run_quality`
    """
    items: list[tuple[Path, Path]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                items.append((p, d))
    return items


def run_quality(
    dirs: list[Path],
    *,
    root: Path,
    device: str = "cuda",
    write_report: bool = True,
    progress: QualityProgress | None = None,
    on_update: Callable[[], None] | None = None,
) -> None:
    """对所选目录执行质量评分，输出 `.quality_scores.json` 和 HTML 报告。

    Args:
        dirs: 绝对路径目录列表。
        root: 数据集根目录。
        device: 推理设备。
        write_report: 是否输出 HTML 报告。
        progress: 进度追踪器。
        on_update: 进度更新回调。

    Callers:
        - `routes.quality`
    """
    root = root.resolve()
    items = _collect(root, dirs)

    if progress is not None:
        progress.total = len(items)
        progress.done = 0
        progress.ok = 0
        progress.skipped = 0
        progress.failed = 0
        progress.status = "running"
        progress.phase = "loading"
        progress.message = f"共 {len(items)} 张，加载 Q-ReAlign 模型…"
        progress.log(f"开始: dirs={[d.name for d in dirs]}, device={device}, total={len(items)}")
        if on_update:
            on_update()

    if not items:
        if progress is not None:
            progress.status = "done"
            progress.phase = "done"
            progress.message = "没有可处理的图片"
            if on_update:
                on_update()
        return

    device = "cuda" if device == "cuda" and torch.cuda.is_available() else "cpu"
    model, processor = _load_model(device)

    if progress is not None:
        progress.phase = "running"
        progress.message = f"共 {len(items)} 张"
        progress.log(f"模型就绪 ({device})")
        if on_update:
            on_update()

    existing = load_quality_scores(root)
    scores: dict[str, float] = existing.copy()
    all_results: list[dict[str, Any]] = []

    for i, (src, _parent) in enumerate(items):
        if progress is not None and progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.message = f"已取消 ({progress.ok} 完成)"
            progress.log(progress.message)
            save_quality_scores(root, scores)
            if on_update:
                on_update()
            return

        rel = str(src.relative_to(root)).replace("\\", "/")
        progress.current = rel if progress is not None else ""

        if rel in existing:
            score = existing[rel]
            tag = quality_to_tag(score)
            all_results.append({"path": rel, "quality": score, "quality_tag": tag})
            if progress is not None:
                progress.done += 1
                progress.skipped += 1
                progress.ok += 1
        else:
            try:
                image = Image.open(src).convert("RGB")
                score = _predict_quality(image, model, processor, device)
                scores[rel] = score
                tag = quality_to_tag(score)
                all_results.append({"path": rel, "quality": score, "quality_tag": tag})
                if progress is not None:
                    progress.done += 1
                    progress.ok += 1
            except (OSError, UnidentifiedImageError, RuntimeError) as e:
                if progress is not None:
                    progress.done += 1
                    progress.failed += 1
                    progress.log(f"失败: {rel} — {e}")
                continue

        if progress is not None and on_update is not None:
            if progress.done % 5 == 0 or progress.done == len(items):
                save_quality_scores(root, scores)
                on_update()

    save_quality_scores(root, scores)

    if write_report and all_results:
        all_results.sort(key=lambda x: x["quality"], reverse=True)
        _generate_html(all_results, root.name, report_path(root), root)

    if progress is not None:
        progress.status = "done"
        progress.phase = "done"
        progress.message = f"完成 ({progress.ok} OK, {progress.failed} 失败)"
        progress.log(progress.message)
        if on_update:
            on_update()
