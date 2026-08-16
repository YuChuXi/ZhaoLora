"""SAM3 文本提示概念分割服务.

使用 ``transformers.Sam3Model`` / ``Sam3Processor`` 加载 Meta 的 Segment
Anything Model 3 (facebook/sam3)，以文本提示对图片进行概念分割，返回该概念
所有实例的并集掩码，并据此计算掩码占比、生成用于瀑布流叠加的半透明 PNG。

权重从 ModelScope 直接下载至本地缓存目录
``~/.cache/modelscope/hub/facebook/sam3``，加载时不再联网。

Callers:
    - ``routes/tag_stats``: 概念分割排序 API
"""

from __future__ import annotations

import base64
import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
from PIL import Image

from services.core.device import register_model
from services.analysis.mask_expression import evaluate, extract_concepts, parse
from services.models import JobSnapshot
from services.state.mask import load_mask

if TYPE_CHECKING:
    import torch
    from transformers import Sam3Model, Sam3Processor

# ModelScope 权重下载至本地缓存目录后从该路径加载
_MODEL_PATH = os.path.expanduser("~/.cache/modelscope/hub/facebook/sam3")

# 概念掩码命中阈值与二值化阈值, 与 transformers 官方示例一致
_SEG_THRESHOLD = 0.5
_MASK_THRESHOLD = 0.5

# 半透明叠加 PNG 的最大边长, 减小数据传输量; 前端按卡片宽度等比缩放对齐原图
_OVERLAY_MAX_SIDE = 512
# 掩码叠加颜色 (RGB) 与不透明度 (0-255)
_OVERLAY_COLOR = (255, 60, 60)
_OVERLAY_ALPHA = 150

# 概念分割单次前向的图片批大小; 输入会被 processor 缩放到统一尺寸, 故可混合分辨率
_BATCH_SIZE = 4


def _load_model() -> tuple[Sam3Model, Sam3Processor, str]:
    """从本地 ModelScope 缓存加载概念分割模型与处理器 (统一加载到 CPU).

    Returns:
        ``(model, processor, device)`` 三元组作为句柄; device 恒为 ``"cpu"``.

    Callers:
        - ``device.register_model`` 注册的 load 回调
    """
    from transformers import Sam3Model, Sam3Processor

    model = Sam3Model.from_pretrained(_MODEL_PATH, trust_remote_code=True)
    model.eval()
    processor = Sam3Processor.from_pretrained(_MODEL_PATH, trust_remote_code=True)
    return model, processor, "cpu"


def _unload_model(handle: object) -> None:
    """释放概念分割模型与处理器, 归还显存.

    Args:
        handle: ``_load_model`` 返回的 ``(model, processor, device)`` 三元组.

    Callers:
        - ``device.register_model`` 注册的 unload 回调
    """
    model, processor, _dev = cast("tuple", handle)
    del model, processor


_MODEL = register_model(_load_model, _unload_model)


def model_ready() -> tuple[bool, str]:
    """检查概念分割模型是否可加载（含权重目录与依赖检查，不实际加载）.

    Returns:
        ``(ok, message)``. ok 为 True 时表示权重就绪、可加载.

    Callers:
        - ``routes/tag_stats:concept_query``
    """
    if not os.path.isdir(_MODEL_PATH):
        return False, f"未找到 ModelScope 下载的模型: {_MODEL_PATH}"
    try:
        from transformers import Sam3Model, Sam3Processor  # noqa: F401
        return True, f"模型就绪: {_MODEL_PATH}"
    except Exception as e:
        return False, f"模型不可用: {e}"


# 概念分割模型句柄: (model, processor, device), 由 auto_release 注入
Sam3Handle = tuple["Sam3Model", "Sam3Processor", str]


def _merge_masks(masks) -> np.ndarray | None:
    """将 post_process 返回的实例掩码张量合并为单张二值掩码.

    取所有实例掩码的并集; 无实例时返回 ``None``.

    Args:
        masks: ``torch.Tensor``, shape ``(N, H, W)``, 或 None.

    Returns:
        ``uint8`` 二值掩码 (H, W), 值域 {0, 255}; 无实例时 ``None``.

    Callers:
        - ``segment_concept_mask``
        - ``segment_concept_masks_batch``
    """
    if masks is None or masks.numel() == 0:
        return None
    masks_np = masks.cpu().numpy()
    union = np.zeros(masks_np.shape[1:], dtype=bool)
    for m in masks_np:
        union |= m.astype(bool)
    return (union * 255).astype(np.uint8)


def segment_concept_mask(handle: Sam3Handle, image: Image.Image, text: str) -> np.ndarray | None:
    """对单张图片执行文本提示概念分割.

    Args:
        handle: ``_load_model`` 返回的 ``(model, processor, device)`` 句柄.
        image: 待分割图片 (RGB).
        text: 概念提示词, 如 "cat" / "yellow school bus".

    Returns:
        ``uint8`` 二值掩码 (H, W), 值域 {0, 255}, 尺寸与原图一致;
        概念未命中时为 ``None``.

    Raises:
        RuntimeError: 模型未就绪.

    Callers:
        - ``services/analysis/sam3:run_concept_query``
    """
    model, processor, device = handle
    import torch

    if image.mode != "RGB":
        image = image.convert("RGB")
    inputs = processor(images=image, text=text, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)
    orig_sizes = inputs.get("original_sizes")
    results = processor.post_process_instance_segmentation(
        outputs,
        threshold=_SEG_THRESHOLD,
        mask_threshold=_MASK_THRESHOLD,
        target_sizes=orig_sizes.tolist() if orig_sizes is not None else None,
    )[0]
    return _merge_masks(results.get("masks"))


def segment_concept_masks_batch(handle: Sam3Handle, images: list[Image.Image], text: str) -> list[np.ndarray | None]:
    """对一批图片执行文本提示概念分割（单次前向）.

    processor 会把同批图片缩放到统一尺寸后一次前向, post_process 依据各图
    ``original_sizes`` 还原为原尺寸掩码; 适合批量场景以减少推理开销.

    Args:
        handle: ``_load_model`` 返回的 ``(model, processor, device)`` 句柄.
        images: 待分割图片列表 (均为 RGB).
        text: 概念提示词.

    Returns:
        与 ``images`` 等长的掩码列表, 每项为 ``uint8`` 二值掩码 (H, W) 或
        ``None``(该图未命中概念).

    Raises:
        RuntimeError: 模型未就绪.

    Callers:
        - ``services/analysis/sam3:run_concept_query``
    """
    model, processor, device = handle
    import torch

    texts = [text] * len(images)
    inputs = processor(images=images, text=texts, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)
    orig_sizes = inputs.get("original_sizes")
    target = orig_sizes.tolist() if orig_sizes is not None else None
    per_image = processor.post_process_instance_segmentation(
        outputs,
        threshold=_SEG_THRESHOLD,
        mask_threshold=_MASK_THRESHOLD,
        target_sizes=target,
    )
    return [_merge_masks(res.get("masks")) for res in per_image]


def mask_ratio(mask: np.ndarray) -> float:
    """计算掩码占比: 掩码像素数 / 全图像素数.

    Args:
        mask: ``segment_concept_mask`` 返回的二值掩码.

    Returns:
        ``[0, 1]`` 区间的占比.

    Callers:
        - ``routes/tag_stats:concept_query``
    """
    h, w = mask.shape
    total = h * w
    return float(np.count_nonzero(mask)) / total if total else 0.0


def overlay_png_base64(mask: np.ndarray) -> str:
    """将二值掩码转为半透明叠加 PNG 的 base64 data URL.

    掩码区域填充 ``_OVERLAY_COLOR`` 并带 ``_OVERLAY_ALPHA`` 不透明度，
    其余区域完全透明; 超出 ``_OVERLAY_MAX_SIDE`` 时按比例缩小，保持宽高比，
    前端以卡片宽度等比缩放对齐原图.

    Args:
        mask: 二值掩码 (H, W).

    Returns:
        ``data:image/png;base64,...`` 字符串.

    Callers:
        - ``routes/tag_stats:concept_query``
    """
    h, w = mask.shape
    scale = min(1.0, _OVERLAY_MAX_SIDE / max(w, h))
    if scale < 1.0:
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        mask = np.array(Image.fromarray(mask).resize((nw, nh), Image.Resampling.NEAREST))
        h, w = mask.shape

    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = _OVERLAY_COLOR
    rgba[..., 3] = np.where(mask > 127, _OVERLAY_ALPHA, 0).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@_MODEL.auto_release
def run_concept_query(
    candidates: list[tuple[str, Path]],
    text: str,
    on_progress: Callable[[int, int], None] | None = None,
    handle: Sam3Handle | None = None,
) -> list[dict]:
    """对候选图片列表分批执行概念分割并汇总结果.

    作为概念分割的推理执行入口, 由 ``auto_release`` 注入模型句柄并管理其生命周期:
    进入时 ``acquire`` 并持有引用, 退出时 ``release`` (引用归零后模型驻留内存供
    复用, 空闲超时才彻底释放).

    以 ``_BATCH_SIZE`` 为一批读取图片并单次前向推理, 减少模型调用次数;
    每批结束后即释放图片对象, 避免一次性占用过多内存.

    Args:
        candidates: ``[(相对路径, 图片绝对路径), ...]``.
        text: 概念提示词.
        on_progress: 进度回调 ``(done, total)``.
        handle: ``_load_model`` 返回的 ``(model, processor, device)`` 句柄,
            由 ``auto_release`` 注入, 调用方无需传.

    Returns:
        按掩码占比降序的结果列表, 每项为
        ``{"path", "ratio", "mask_data"}``; 未命中概念的图片
        ``ratio`` 为 0 且 ``mask_data`` 为空字符串.

    Callers:
        - ``routes/tag_stats:concept_query``
    """
    assert handle is not None
    n = len(candidates)
    items: list[dict] = []
    for start in range(0, n, _BATCH_SIZE):
        batch = candidates[start:start + _BATCH_SIZE]
        images = [Image.open(abs_path).convert("RGB") for _, abs_path in batch]
        masks = segment_concept_masks_batch(handle, images, text)
        for (rel, _abs_path), mask in zip(batch, masks):
            if mask is None:
                items.append({"path": rel, "ratio": 0.0, "mask_data": ""})
            else:
                items.append(
                    {
                        "path": rel,
                        "ratio": mask_ratio(mask),
                        "mask_data": overlay_png_base64(mask),
                    }
                )
        del images, masks  # 及时释放图片与前向输出
        if on_progress:
            on_progress(min(start + _BATCH_SIZE, n), n)
    items.sort(key=lambda it: it["ratio"], reverse=True)
    return items


@dataclass
class Sam3MaskProgress:
    """SAM3 表达式蒙版生成进度. 引用: ``routes/birefnet.py:mask_generate``. """

    done: int = 0
    total: int = 0
    failed: int = 0
    cancel_requested: bool = False

    def snapshot(self) -> JobSnapshot:
        """当前进度快照. 引用: ``routes/birefnet.py:mask_generate``. """
        return JobSnapshot(
            status="running",
            kind="sam3_mask",
            dataset="",
            job_id="",
            done=self.done,
            total=self.total,
            failed=self.failed,
            message=f"{self.done}/{self.total}" if self.total else "准备中",
        )


@_MODEL.auto_release
def run_sam3_expression_mask(
    image_rel_paths: list[tuple[str, Path]],
    mask_dir: Path,
    expression: str,
    *,
    handle: Sam3Handle | None = None,
    skip_existing: bool = True,
    progress: Sam3MaskProgress | None = None,
    on_progress: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """对图片列表按逻辑表达式生成蒙板并保存.

    流程:
    1. 解析表达式提取概念列表, 并过滤掉 ``skip_existing`` 命中的图片.
    2. 逐概念分批推理 (SAM3 ``segment_concept_masks_batch``), 得到各图概念掩码.
    3. 逐图加载默认蒙板 (已保存的蒙板, 无则全不透明 255), 求值并保存.

    概念掩码经 SAM3 ``original_sizes`` 还原为原图尺寸, 与默认蒙板尺寸一致.

    Args:
        image_rel_paths: ``[(rel, abs_path), ...]`` 相对数据集根路径与绝对路径.
        mask_dir: 蒙板输出目录.
        expression: 逻辑表达式, 如 ``(默认蒙板-("白色背景"+"对话气泡"))``.
        handle: SAM3 模型句柄, 由 ``auto_release`` 注入.
        skip_existing: 如果输出文件已存在则跳过.
        progress: 进度对象.
        on_progress: 进度回调, 每处理一张后调用.

    Returns:
        (成功数, 总数).

    Callers:
        - ``routes/birefnet.py:mask_generate``
    """
    assert handle is not None
    total = len(image_rel_paths)
    if total == 0:
        return 0, 0

    ast = parse(expression)
    concepts = extract_concepts(ast)

    if progress is not None:
        progress.total = total
        progress.done = 0
        progress.failed = 0

    # 过滤跳过已有蒙板的图片, 避免无谓推理; 已存在的计入 done, 使进度可达 total
    skipped = 0
    pending: list[tuple[str, Path]] = []
    for rel, abs_path in image_rel_paths:
        if skip_existing and (mask_dir / Path(rel).with_suffix(".png")).is_file():
            skipped += 1
        else:
            pending.append((rel, abs_path))

    if progress is not None:
        progress.total = total
        progress.done = skipped
        progress.failed = 0

    # 1) 为每个概念生成掩码: concept -> {rel: mask | None}
    concept_masks: dict[str, dict[str, np.ndarray | None]] = {}
    for text in concepts:
        per_rel: dict[str, np.ndarray | None] = {}
        for start in range(0, len(pending), _BATCH_SIZE):
            batch = pending[start:start + _BATCH_SIZE]
            images = [Image.open(abs_path).convert("RGB") for _rel, abs_path in batch]
            masks = segment_concept_masks_batch(handle, images, text)
            for (rel, _abs), mask in zip(batch, masks):
                per_rel[rel] = mask
            del images, masks  # 及时释放图片与前向输出
        concept_masks[text] = per_rel

    # 2) 逐图组装并求值: 成功计入 done, 失败计入 failed, 跳过计入 done 起点
    done = skipped
    failed = 0
    for rel, abs_path in pending:
        if progress is not None and progress.cancel_requested:
            break

        try:
            img_size = Image.open(abs_path).size  # (W, H)
            default = load_mask(mask_dir, rel, img_size)
            if default is None:
                default = np.full((img_size[1], img_size[0]), 255, dtype=np.uint8)

            per_concept = {text: concept_masks[text].get(rel) for text in concepts}
            result = evaluate(ast, per_concept, default)

            out_path = mask_dir / Path(rel).with_suffix(".png")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            white = Image.new("L", (img_size[0], img_size[1]), 255)
            Image.merge(
                "RGBA", (white, white, white, Image.fromarray(result, mode="L"))
            ).save(out_path, format="PNG")
            done += 1
        except Exception:
            failed += 1

        if progress is not None:
            progress.done = done
            progress.failed = failed
        if on_progress:
            on_progress()

    return done, total
