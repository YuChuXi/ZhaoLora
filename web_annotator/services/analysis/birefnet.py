"""BiRefNet 背景移除蒙版生成, 由 JobManager 管理进度. """

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import cv2
import numpy as np
import torch
from PIL import Image
from torchvision import transforms

from services.models import JobSnapshot
from services.core.scanner import IMAGE_EXTS
from services.core.device import register_model

logger = logging.getLogger(__name__)

MODEL_REPO = "ZhengPeng7/BiRefNet"
IMAGE_SIZE = (1024, 1024)
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]

def _load_model() -> tuple:
    """加载 BiRefNet 模型与预处理变换 (CPU).

    Callers:
        - ``services/core/device.py:register_model`` 注册的 load 回调
    """
    try:
        from transformers import AutoModelForImageSegmentation, AutoConfig
    except ImportError:
        raise RuntimeError("需要 transformers: pip install transformers huggingface_hub")

    weight_path = os.environ.get(
        "BIREFNET_WEIGHT",
        "/media/yuchuxi/Data/Servers/ComfyUI/models/background_removal/birefnet.safetensors",
    )
    config = AutoConfig.from_pretrained(MODEL_REPO, trust_remote_code=True)
    config.torch_dtype = torch.float32
    model = AutoModelForImageSegmentation.from_config(config, trust_remote_code=True)

    if weight_path and os.path.isfile(weight_path):
        try:
            import safetensors.torch
            state_dict = safetensors.torch.load_file(weight_path)
        except (ImportError, OSError, RuntimeError):
            state_dict = torch.load(weight_path, map_location="cpu", weights_only=True)
        for k in list(state_dict.keys()):
            for prefix in ("module.", "_orig_mod."):
                if k.startswith(prefix):
                    state_dict[k[len(prefix):]] = state_dict.pop(k)
        model.load_state_dict(state_dict, strict=False)
    else:
        model = AutoModelForImageSegmentation.from_pretrained(
            MODEL_REPO, trust_remote_code=True, dtype=torch.float32,
        )

    model = model.to("cpu").eval()
    transform = transforms.Compose([
        transforms.Resize(IMAGE_SIZE),
        transforms.ToTensor(),
        transforms.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
    ])
    return model, transform


def _unload_model(handle: object) -> None:
    """释放 BiRefNet 模型.

    Args:
        handle: ``_load_model`` 返回的 ``(model, transform)`` 元组.

    Callers:
        - ``services/core/device.py:register_model`` 注册的 unload 回调
    """
    model, _transform = cast("tuple", handle)
    del model, _transform


_MODEL = register_model(_load_model, _unload_model)


def _predict_mask(model, image: Image.Image, transform) -> np.ndarray:
    """对单张图片推理, 返回 [0, 255] 的 uint8 mask (H, W).

    Args:
        model: BiRefNet 模型.
        image: 输入图片.
        transform: 预处理变换.

    Returns:
        ``uint8`` 蒙版 (H, W), 值域 [0, 255].

    Callers:
        - ``services/analysis/birefnet.py:run_birefnet_mask``
    """
    device = next(model.parameters()).device
    orig_size = image.size
    input_tensor = transform(image).unsqueeze(0).to(device)
    with torch.no_grad():
        preds = model(input_tensor)
        pred = preds[-1].sigmoid().cpu() if isinstance(preds, (list, tuple)) else preds.sigmoid().cpu()
    pred = pred[0].squeeze()
    if pred.dim() == 3:
        pred = pred.squeeze(0)
    pred_pil = transforms.ToPILImage()(pred)
    pred_pil = pred_pil.resize(orig_size, Image.Resampling.BILINEAR)
    return np.array(pred_pil, dtype=np.uint8)


def _expand_mask(mask: np.ndarray, expand_px: int) -> np.ndarray:
    """将 mask 主体向外扩展指定像素数. 

    Callers:
        - ``services/analysis/birefnet.py:run_birefnet_mask``
    """
    if expand_px <= 0:
        return mask
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (expand_px * 2 + 1, expand_px * 2 + 1))
    return cv2.dilate(mask, k, iterations=1)


def _shrink_mask(mask: np.ndarray, shrink_px: int) -> np.ndarray:
    """将 mask 主体向内收缩指定像素数. 

    Callers:
        - ``services/analysis/birefnet.py:run_birefnet_mask``
    """
    if shrink_px <= 0:
        return mask
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (shrink_px * 2 + 1, shrink_px * 2 + 1))
    return cv2.erode(mask, k, iterations=1)


def _remap_mask(mask: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """将 mask 值从 [0, 255] 线性映射到 [lo, hi]. 

    Callers:
        - ``services/analysis/birefnet.py:run_birefnet_mask``
    """
    if lo == 0 and hi == 255:
        return mask
    f = mask.astype(np.float32) / 255.0
    return np.clip(f * (hi - lo) + lo, 0, 255).astype(np.uint8)


def collect_images(data_dir: Path, subdir: str = "main") -> list[str]:
    """收集指定子目录下所有图片的相对数据集根路径(含子目录前缀). 

    Args:
        data_dir: 数据集根目录. 
        subdir: 子目录名, 默认 ``main``. 

    Returns:
        相对数据集根的路径列表, 如 ``main/foo.png`` / ``head/foo.png``. 

    Callers:
        - ``routes/birefnet.py:mask_generate``
    """
    subdir = (subdir or "main").strip().strip("/")
    target = data_dir / subdir
    if not target.is_dir():
        return []
    paths: list[str] = []
    for img in sorted(target.iterdir()):
        if not img.is_file() or img.suffix.lower() not in IMAGE_EXTS:
            continue
        paths.append(f"{subdir}/{img.name}")
    return paths


@dataclass
class BirefnetProgress:
    """BiRefNet 蒙版生成进度. """

    done: int = 0
    total: int = 0
    failed: int = 0
    cancel_requested: bool = False

    def snapshot(self) -> JobSnapshot:
        """当前进度快照. 

        Callers:
            - ``routes/birefnet.py:mask_generate``
        """
        return JobSnapshot(
            status="running",
            kind="birefnet_mask",
            dataset="",
            job_id="",
            done=self.done,
            total=self.total,
            failed=self.failed,
            message=f"{self.done}/{self.total}" if self.total else "准备中",
        )


@_MODEL.auto_release
def run_birefnet_mask(
    image_rel_paths: list[str],
    data_dir: Path,
    mask_dir: Path,
    *,
    handle: tuple,
    mask_expand: int = 32,
    mask_shrink: int = 0,
    mask_min: int = 96,
    mask_max: int = 255,
    skip_existing: bool = True,
    progress: BirefnetProgress | None = None,
    on_progress: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """对图片列表生成 BiRefNet 蒙版, 输出到 mask_dir(保留子目录结构).

    Args:
        image_rel_paths: 相对数据集根的路径列表(含子目录前缀).
        data_dir: 数据集根目录.
        mask_dir: 蒙版输出目录.
        handle: BiRefNet 模型句柄 ``(model, transform)``, 由 ``auto_release`` 注入.
        mask_expand: 蒙版扩展像素数 (0-200).
        mask_shrink: 蒙版收缩像素数 (0-200).
        mask_min: 蒙版值域下限 (0-255, 默认 96).
        mask_max: 蒙版值域上限 (0-255, 默认 255).
        skip_existing: 是否跳过已有蒙版文件(默认 True).

    Returns:
        (成功数, 总数).

    Callers:
        - ``routes/birefnet.py:mask_generate``
    """
    if not image_rel_paths:
        return 0, 0

    model, transform = handle
    mask_dir.mkdir(parents=True, exist_ok=True)

    if progress is not None:
        progress.total = len(image_rel_paths)
        progress.done = 0
        progress.failed = 0

    for rel_path in image_rel_paths:
        if progress is not None and progress.cancel_requested:
            break

        src_path = data_dir / rel_path
        out_path = mask_dir / Path(rel_path).with_suffix(".png")

        if skip_existing and out_path.is_file():
            if progress is not None:
                progress.done += 1
                if on_progress:
                    on_progress()
            continue

        try:
            image = Image.open(src_path).convert("RGB")
            mask = _predict_mask(model, image, transform)
            mask = _expand_mask(mask, mask_expand)
            mask = _shrink_mask(mask, mask_shrink)
            mask = _remap_mask(mask, mask_min, mask_max)
            Image.fromarray(mask).save(out_path)
            if progress is not None:
                progress.done += 1
        except Exception as e:
            logger.error("BiRefNet 处理失败 %s: %s", rel_path, e)
            if progress is not None:
                progress.failed += 1

        if progress is not None and on_progress is not None:
            on_progress()

    return (progress.done if progress else 0, len(image_rel_paths))
