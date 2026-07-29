"""统一评分引擎: 美学(CLIP)与质量(Q-ReAlign)共用基础设施. """

from __future__ import annotations

import json
import threading as _th
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn
from huggingface_hub import hf_hub_download
from PIL import Image, UnidentifiedImageError

from services.models import JobSnapshot
from services.core.scanner import IMAGE_EXTS, collect_images

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

AESTHETIC_SCORES = ".aesthetic_scores.json"
QUALITY_SCORES = ".quality_scores.json"
QUALITY_TAGS = [
    "worst quality", "low quality", "normal quality",
    "good quality", "best quality", "masterpiece",
]

# ---------------------------------------------------------------------------
# 进度追踪
# ---------------------------------------------------------------------------


@dataclass
class ScoreProgress:
    """通用评分进度追踪. 

    通过 ``snapshot()`` 生成 ``JobSnapshot`` 推送至 SSE. 

    Attributes:
        status: 当前状态 ("idle", "running", "done", "cancelled"). 
        phase: 当前阶段 ("idle", "loading", "running", "done"). 
        total: 待处理图片总数. 
        done: 已处理数量. 
        ok: 成功评分数量. 
        skipped: 跳过(已有缓存)数量. 
        failed: 失败数量. 
        current: 当前处理的图片相对路径. 
        message: 用户可见的状态消息. 
        logs: 内部日志行列表(最多 400 条). 
        cancel_requested: 是否已请求取消. 
    """

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
        """生成可序列化的进度快照. 

        Returns:
            JobSnapshot: 包含完成百分比与最近 80 条日志的快照. 

        Callers:
            - ``routes/scoring.py``: SSE 推送评分进度. 
        """
        total, done = self.total, self.done
        pct = round(100.0 * done / total, 1) if total > 0 else 0.0
        if self.status == "done" and total > 0:
            pct = 100.0
        return JobSnapshot(
            status=self.status, phase=self.phase, total=total, done=done, pct=pct,
            ok=self.ok, skipped=self.skipped, failed=self.failed,
            current=self.current, message=self.message,
            logs=list(self.logs[-80:]), cancel_requested=self.cancel_requested,
        )

    def log(self, line: str) -> None:
        """追加一行日志, 保持最多 400 行上限. 

        Args:
            line: 日志文本. 
        """
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-300:]


# ---------------------------------------------------------------------------
# 分数 IO
# ---------------------------------------------------------------------------


def load_score_json(root: Path, filename: str) -> dict[str, float]:
    """读取数据集根目录下的分数 JSON 文件. 

    返回 ``{相对路径: 分数}`` 的映射字典；文件不存在时返回空字典. 
    自动规范化路径分隔符. 

    Args:
        root: 数据集根目录. 
        filename: JSON 文件名(如 ``.aesthetic_scores.json``). 

    Returns:
        dict[str, float]: 相对路径到分数的映射. 

    Raises:
        TypeError: JSON 根节点不是对象时抛出. 

    Callers:
        - ``routes/images.py``: 加载美学/质量分数填充图片列表. 
        - ``routes/tag_stats.py``: 加载分数用于标签注入. 
        - ``run_scoring``: 读取已有分数跳过重复计算. 
    """
    path = root.resolve() / filename
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"分数文件根节点必须是对象: {path}")
    return {str(k).replace("\\", "/").lstrip("/"): float(v) for k, v in data.items()}


def save_score_json(root: Path, filename: str, scores: dict[str, float]) -> None:
    """将分数写入数据集根目录下的 JSON 文件. 

    自动规范化路径分隔符, 分数保留 6 位小数, 按 key 排序输出. 

    Args:
        root: 数据集根目录. 
        filename: JSON 文件名. 
        scores: 相对路径到分数的映射. 

    Callers:
        - ``run_scoring``: 评分过程中定期持久化. 
    """
    path = root.resolve() / filename
    clean = {str(k).replace("\\", "/").lstrip("/"): round(float(v), 6) for k, v in scores.items()}
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")




# ---------------------------------------------------------------------------
# 美学模型(CLIP ViT-L/14 + purplesmartai aesthetic-classifier)
# ---------------------------------------------------------------------------

_AES_MODELS: dict[str, tuple] = {}
_AES_LOCK = _th.Lock()


def _raw_to_bin(raw: float) -> int:
    """原始美学分 [0, 1] → 0~9 整数档位. 

    Args:
        raw: CLIP 美学头输出的原始分数. 

    Returns:
        int: 0~9 的整数档位. 

    Callers:
        - ``build_score_results``
    """
    return int(max(0.0, min(0.99, raw)) * 10)


@torch.no_grad()
def _aes_score_one(image: Image.Image, clip_model, preprocess, aes_head, device: str) -> float:
    """对单张图片执行 CLIP + 美学头评分. 

    Args:
        image: PIL 图片对象. 
        clip_model: CLIP ViT-L/14 模型. 
        preprocess: CLIP 预处理函数. 
        aes_head: 美学评分 MLP 头. 
        device: 计算设备. 

    Returns:
        float: 原始美学分数(约 0~1). 

    Callers:
        - ``_run_scoring`` (via ``score_one`` lambda)
    """
    t = preprocess(image.convert("RGB")).unsqueeze(0).to(device)
    feat = clip_model.encode_image(t).cpu().numpy().astype("float32")
    norm = np.linalg.norm(feat, axis=1, keepdims=True)
    feat = feat / np.where(norm == 0, 1, norm)
    return float(aes_head(torch.tensor(feat, device=device)).item())


def _load_aes_models(device: str) -> tuple:
    """加载 CLIP ViT-L/14 + 美学 MLP 头, 按设备缓存. 

    若设备为 ``cuda`` 但 CUDA 不可用则回退至 ``cpu``. 线程安全. 

    Args:
        device: 目标设备字符串. 

    Returns:
        tuple: ``(clip_model, preprocess_fn, aes_head, actual_device)``. 

    Callers:
        - ``_run_scoring`` (via ``load_models``)
    """
    device = "cuda" if device == "cuda" and torch.cuda.is_available() else "cpu"
    with _AES_LOCK:
        if device in _AES_MODELS:
            return _AES_MODELS[device]
        import clip
        clip_model, preprocess = clip.load("ViT-L/14", device=device)
        clip_model.eval()
        ckpt_path = hf_hub_download(repo_id="purplesmartai/aesthetic-classifier", filename="v2.ckpt")
        state_dict = torch.load(ckpt_path, map_location=device)["state_dict"]
        state_dict = {k.replace("model.", ""): v for k, v in state_dict.items()}
        aes_head = nn.Sequential(
            nn.Linear(768, 1024), nn.ReLU(), nn.Dropout(0.5),
            nn.Linear(1024, 512), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(512, 1),
        ).to(device)
        aes_head.load_state_dict(state_dict)
        aes_head.eval()
        _AES_MODELS[device] = (clip_model, preprocess, aes_head, device)
        return _AES_MODELS[device]


# ---------------------------------------------------------------------------
# 质量模型(Q-ReAlign-Lite-4B)
# ---------------------------------------------------------------------------

_q_model: tuple | None = None


def _load_q_models(device: str) -> tuple:
    """加载 Q-ReAlign-Lite-4B 模型, 模块级缓存. 

    Args:
        device: 目标设备字符串. 

    Returns:
        tuple: ``(model, processor)``. 

    Raises:
        RuntimeError: transformers 版本不足时抛出. 

    Callers:
        - ``_run_scoring`` (via ``load_models``)
    """
    global _q_model
    if _q_model is not None:
        return _q_model
    try:
        from transformers import AutoModelForImageTextToText, AutoProcessor
    except ImportError:
        raise RuntimeError("需要 transformers >= 5.2.0")
    processor = AutoProcessor.from_pretrained("q-future/Q-ReAlign-Lite-4B")
    model = AutoModelForImageTextToText.from_pretrained(
        "q-future/Q-ReAlign-Lite-4B", dtype="auto",
    ).to(device).eval()
    _q_model = (model, processor)
    return _q_model


@torch.no_grad()
def _q_score_one(src: Path, models: tuple, device: str) -> float:
    """对单张图片执行 Q-ReAlign 质量评分. 

    Args:
        src: 图片文件路径. 
        models: ``(model, processor)`` 元组. 
        device: 计算设备. 

    Returns:
        float: 加权质量分(0~1). 

    Callers:
        - ``_run_scoring`` (via ``score_one``)
    """
    model, processor = models
    img = Image.open(src)
    levels = ["excellent", "good", "fair", "poor", "bad"]
    weights = [1.0, 0.75, 0.5, 0.25, 0.0]
    messages = [{"role": "user", "content": [
        {"type": "image"},
        {"type": "text", "text": "How would you rate the quality of this image?"},
    ]}]
    text = processor.apply_chat_template(messages, add_generation_prompt=True)
    text += "The quality of the image is"
    inputs = processor(text=[text], images=[img.convert("RGB")], return_tensors="pt").to(device)
    ids = [processor.tokenizer(" " + w, add_special_tokens=False).input_ids[0] for w in levels]
    probs = model(**inputs).logits[0, -1, ids].softmax(-1)
    return float((probs * torch.tensor(weights, device=device)).sum().item())


def quality_to_tag(score: float) -> str:
    """将连续质量分映射为 6 级质量标签. 

    等宽分箱: 

    - [0.00, 0.17) → ``worst quality``
    - [0.17, 0.33) → ``low quality``
    - [0.33, 0.50) → ``normal quality``
    - [0.50, 0.67) → ``good quality``
    - [0.67, 0.83) → ``best quality``
    - [0.83, 1.00] → ``masterpiece``

    Args:
        score: 质量分数, 范围 [0, 1]. 

    Returns:
        str: 对应的质量标签. 

    Callers:
        - ``build_score_results``
        - ``services/tagging/clean.py:clean_caption``: 标签清理时注入质量标签. 
    """
    i = min(int(score * len(QUALITY_TAGS)), len(QUALITY_TAGS) - 1)
    return QUALITY_TAGS[max(0, i)]


def load_quality_scores(root: Path) -> dict[str, float]:
    """读取数据集根目录下的质量分数 JSON 文件. 

    Args:
        root: 数据集根目录. 

    Returns:
        dict[str, float]: 相对路径到质量分的映射, 文件不存在时返回空字典. 

    Callers:
        - ``routes/images.py``: 填充图片列表的质量字段. 
        - ``routes/tag_stats.py``: 标签操作时加载质量分. 
    """
    return load_score_json(root, QUALITY_SCORES)


# ---------------------------------------------------------------------------
# 评分引擎
# ---------------------------------------------------------------------------


def _run_scoring(
    dirs: list[Path], *,
    root: Path,
    device: str,
    scores_filename: str,
    model_name: str,
    load_models: Callable[[str], Any],
    score_one: Callable[[Path, Any, str], float],
    progress: ScoreProgress,
    on_update: Callable[[], None] | None,
) -> None:
    """评分主循环引擎. 

    处理完整的评分流水线: 文件收集 → 进度初始化 → 模型加载 →
    缓存跳过/新评计算 → 定期持久化. 

    Args:
        dirs: 要评分的子目录列表(自动去重). 
        root: 数据集根目录(仅关键字参数). 
        device: 计算设备(仅关键字参数). 
        scores_filename: 分数 JSON 文件名(仅关键字参数). 
        model_name: 模型名称, 用于进度消息(仅关键字参数). 
        load_models: ``(device) -> model_state`` 模型加载回调(仅关键字参数). 
        score_one: ``(src_path, model_state, device) -> float`` 单张评分回调(仅关键字参数). 
        progress: 进度追踪实例(仅关键字参数). 
        on_update: 每次状态变更后的回调(仅关键字参数). 

    Callers:
        - ``run_scoring``
    """
    root = root.resolve()
    items = collect_images(list(dict.fromkeys(dirs)))
    progress.total = len(items)
    progress.done = progress.ok = progress.skipped = progress.failed = 0
    progress.status = "running"
    progress.phase = "loading"
    progress.message = f"共 {len(items)} 张, 加载{model_name}模型…"
    progress.log(f"开始: dirs={[d.name for d in dirs]}, device={device}, total={len(items)}")
    if on_update: on_update()

    if not items:
        progress.status = progress.phase = "done"
        progress.message = "没有可处理的图片"
        if on_update: on_update()
        return

    device = "cuda" if device == "cuda" and torch.cuda.is_available() else "cpu"
    models = load_models(device)
    progress.phase = "running"
    progress.message = f"共 {len(items)} 张"
    progress.log(f"{model_name}模型就绪 ({device})")
    if on_update: on_update()

    existing = load_score_json(root, scores_filename)
    scores: dict[str, float] = existing.copy()

    for src in items:
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.message = f"已取消 ({progress.ok} 完成)"
            save_score_json(root, scores_filename, scores)
            if on_update: on_update()
            return

        rel = str(src.relative_to(root)).replace("\\", "/")
        progress.current = rel

        if rel in existing:
            progress.skipped += 1
            progress.ok += 1
            progress.done += 1
        else:
            try:
                scores[rel] = score_one(src, models, device)
                progress.ok += 1
                progress.done += 1
            except (OSError, UnidentifiedImageError, RuntimeError, ValueError) as e:
                progress.failed += 1
                progress.done += 1
                progress.log(f"失败: {rel} — {e}")

        if progress.done % 10 == 0 or progress.done == len(items):
            save_score_json(root, scores_filename, scores)
        if on_update: on_update()

    save_score_json(root, scores_filename, scores)
    progress.status = progress.phase = "done"
    progress.current = ""
    progress.message = f"完成 {progress.ok} 张, 跳过 {progress.skipped}"
    if on_update: on_update()


# ---------------------------------------------------------------------------
# 统一评分入口
# ---------------------------------------------------------------------------


def run_scoring(
    dirs: list[Path], *,
    kind: str,
    root: Path,
    device: str = "cuda",
    progress: ScoreProgress | None = None,
    on_update: Callable[[], None] | None = None,
) -> None:
    """执行美学或质量评分. 

    Args:
        dirs: 要评分的子目录列表. 
        kind: 评分类型, ``"aesthetic"`` 或 ``"quality"``(仅关键字参数). 
        root: 数据集根目录(仅关键字参数). 
        device: 计算设备(仅关键字, 默认 "cuda"). 
        progress: 进度追踪实例(仅关键字, 为 None 时自动创建). 
        on_update: 每次状态变更后的回调(仅关键字). 

    Raises:
        ValueError: ``kind`` 不是 ``"aesthetic"`` 或 ``"quality"`` 时抛出. 

    Callers:
        - ``routes/scoring.py``
    """
    if kind not in ("aesthetic", "quality"):
        raise ValueError(f"kind 必须是 'aesthetic' 或 'quality', 收到: {kind!r}")
    if progress is None:
        progress = ScoreProgress()

    if kind == "aesthetic":
        _run_scoring(
            dirs, root=root, device=device,
            scores_filename=AESTHETIC_SCORES, model_name="美学",
            load_models=_load_aes_models,
            score_one=lambda src, models, dev: _aes_score_one(
                Image.open(src), models[0], models[1], models[2], dev,
            ),
            progress=progress, on_update=on_update,
        )
    else:
        _run_scoring(
            dirs, root=root, device=device,
            scores_filename=QUALITY_SCORES, model_name="Q-ReAlign",
            load_models=_load_q_models,
            score_one=_q_score_one,
            progress=progress, on_update=on_update,
        )


# ---------------------------------------------------------------------------
# 查询结果构建(供 API 返回 JSON)
# ---------------------------------------------------------------------------


def build_score_results(
    root: Path, kind: str,
) -> list[dict[str, Any]]:
    """从缓存的分数 JSON 构建前端的评分结果列表. 

    Args:
        root: 数据集根目录. 
        kind: 评分类型, ``"aesthetic"`` 或 ``"quality"``. 

    Returns:
        list[dict]: 每条包含 ``path``、``score`` 及类型专用字段, 
        按分数降序排列. 

    Callers:
        - ``routes/scoring.py:scoring_results``
    """
    if kind == "aesthetic":
        scores = load_score_json(root, AESTHETIC_SCORES)
        results: list[dict[str, Any]] = []
        for path, raw in scores.items():
            results.append({
                "path": path,
                "score": round(raw, 6),
                "pony": _raw_to_bin(raw),
                "pony_label": f"score_{_raw_to_bin(raw)}",
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results
    else:
        scores = load_score_json(root, QUALITY_SCORES)
        results = []
        for path, sc in scores.items():
            results.append({
                "path": path,
                "score": round(sc, 6),
                "quality_tag": quality_to_tag(sc),
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results

