"""Qwen3-VL-Reranker-2B 多模态重排序服务.

使用 ``sentence_transformers.CrossEncoder`` 加载模型，对 (查询文本, 图片)
配对实时计算相似度。Reranker 为 cross-encoder，无法像 bi-encoder 那样预计算
向量索引，故每次查询须对全部候选图片实时评分，不依赖任何缓存。

Callers:
    - ``routes/tag_stats.py:`` : 查询排序、批量标记 API
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from services.core.device import cuda_device, register_model

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

_MODEL_NAME = "Qwen/Qwen3-VL-Reranker-2B"

# instruction-aware 提示词, 用于将任务限定为「按查询检索图片」, 提升检索相关性
_PROMPT = "Retrieve images or text relevant to the user's query."

# 单次 predict 的配对数量; Reranker 须逐张前向计算, 分批避免一次性占用过多显存
_BATCH_SIZE = 8


def _load_model() -> CrossEncoder:
    """从 HuggingFace 加载重排序模型 (恒载入 CPU 内存).

    ``huggingface_hub`` 下载模型时遵循 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量.
    转移目标设备由 ``device.Model`` 生命周期统一决定, 加载侧不做设备判断.

    Returns:
        CrossEncoder 实例.

    Callers:
        - ``services/core/device.py:register_model`` 注册的 load 回调
    """
    from sentence_transformers import CrossEncoder

    return CrossEncoder(
        _MODEL_NAME,
        trust_remote_code=True,
        device="cpu",
    )


def _unload_model(model: object) -> None:
    """释放重排序模型, 归还显存.

    Args:
        model: 待卸载的 CrossEncoder 实例 (device 以 object 形式传入).

    Callers:
        - ``services/core/device.py:register_model`` 注册的 unload 回调
    """
    del model


_MODEL = register_model(_load_model, _unload_model)


def model_ready() -> tuple[bool, str]:
    """检查重排序模型是否可用（含依赖与 CUDA 检测，不实际加载）.

    Returns:
        ``(ok, message)``. ok 为 True 时表示依赖就绪、可加载.

    Callers:
        - ``routes/tag_stats.py:embedding_status``
        - ``routes/tag_stats.py:embedding_query``
    """
    try:
        from sentence_transformers import CrossEncoder  # noqa: F401
        cuda_device()
        return True, f"模型就绪: {_MODEL_NAME}"
    except Exception as e:
        return False, f"模型不可用: {e}"


@_MODEL.auto_release
def rerank(
    query_text: str,
    candidates: list[tuple[str, Path]],
    on_progress: Callable[[int, int], None] | None = None,
    handle: CrossEncoder | None = None,
) -> list[tuple[str, float]]:
    """对候选图片与查询文本逐张实时计算相似度.

    作为相似度评分的推理执行入口, 由 ``auto_release`` 注入模型句柄并管理其
    生命周期: 进入时 ``acquire`` 并持有引用, 退出时 ``release`` (引用归零后
    模型驻留内存供复用, 空闲超时才彻底释放).

    因 Reranker 为 cross-encoder，须对每张候选图构造 (query, image) 配对并
    执行前向计算，无法复用预计算缓存。分批调用模型并逐批上报进度，
    避免一次性占用过多显存。

    Args:
        query_text: 查询文本。
        candidates: ``[(相对路径, 图片绝对路径), ...]``。
        on_progress: 进度回调 ``(done, total)``, 每完成一批调用一次。
        handle: CrossEncoder 实例, 由 ``auto_release`` 注入, 调用方无需传.

    Returns:
        ``[(相对路径, 相似度), ...]``，按相似度降序排列。相似度经 sigmoid
        映射到 [0, 1]，与前端阈值语义一致。

    Callers:
        - ``routes/tag_stats.py:embedding_query``
    """
    assert handle is not None
    model = handle

    # 构造 (query, image) 配对; 图片以本地路径 dict 形式传入, 由模型自行加载
    pairs: list[tuple[str, dict[str, str]]] = [
        (query_text, {"image": str(abs_path)})
        for _, abs_path in candidates
    ]

    import torch

    n = len(pairs)
    scores: list[float] = []
    for start in range(0, n, _BATCH_SIZE):
        end = min(start + _BATCH_SIZE, n)
        batch = pairs[start:end]
        # sigmoid 将原始分数映射到 [0, 1], 与前端阈值语义一致
        batch_scores = model.predict(
            batch, prompt=_PROMPT, activation_fn=torch.nn.Sigmoid()
        )
        scores.extend(float(s) for s in batch_scores)
        if on_progress:
            on_progress(end, n)

    # 按分数降序排列
    ranked = sorted(zip(candidates, scores), key=lambda t: t[1], reverse=True)
    return [(rel, score) for (rel, _), score in ranked]
