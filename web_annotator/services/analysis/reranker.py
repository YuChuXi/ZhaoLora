"""Qwen3-VL-Reranker-2B 多模态重排序服务.

使用 ``sentence_transformers.CrossEncoder`` 加载模型，对 (查询文本, 图片) 配对
实时计算相关性分数。Reranker 是 cross-encoder，无法像 bi-encoder 那样预计算
向量索引，因此每次查询都对候选图片实时打分，不依赖任何缓存。

Callers:
    - ``routes/tag_stats``: 查询排序、批量标记 API
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from services.core.device import cuda_device

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

_MODEL_NAME = "Qwen/Qwen3-VL-Reranker-2B"

# instruction-aware 提示词, 用于将任务限定为「按查询检索图片」, 提升相关性精度
_PROMPT = "Retrieve images or text relevant to the user's query."

# 单次 predict 的配对数量; Reranker 需逐图前向, 分批避免一次性占用过多显存
_BATCH_SIZE = 8

_RERANK_MODEL: CrossEncoder | None = None
_RERANK_LOCK = threading.Lock()


def model_ready() -> tuple[bool, str]:
    """检查重排序模型是否已加载，未加载时从 HuggingFace 加载。

    ``huggingface_hub`` 下载模型时遵循 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量。

    Returns:
        ``(ok, message)``. ok 为 True 时表示模型可用。

    Callers:
        - ``routes/tag_stats:embedding_status``
        - ``routes/tag_stats:embedding_query``
    """
    global _RERANK_MODEL

    if _RERANK_MODEL is not None:
        return True, "模型已加载"

    with _RERANK_LOCK:
        if _RERANK_MODEL is not None:
            return True, "模型已加载"
        try:
            from sentence_transformers import CrossEncoder

            device = cuda_device()
            model_kwargs: dict = {}
            if device is not None:
                model_kwargs["device"] = str(device)

            _RERANK_MODEL = CrossEncoder(
                _MODEL_NAME,
                trust_remote_code=True,
                **model_kwargs,
            )
            return True, f"模型加载成功: {_MODEL_NAME}"
        except Exception as e:
            return False, f"模型加载失败: {e}"


def _get_model() -> CrossEncoder:
    """获取已加载的模型（确保已调用 ``model_ready``）。

    Returns:
        CrossEncoder 实例。

    Callers:
        - ``services/analysis/reranker:rerank``
    """
    return cast("CrossEncoder", _RERANK_MODEL)


def rerank(
    query_text: str,
    candidates: list[tuple[str, Path]],
    on_progress: Callable[[int, int], None] | None = None,
) -> list[tuple[str, float]]:
    """对候选图片与查询文本逐张实时计算相关性分数。

    因 Reranker 为 cross-encoder，须对每张候选图构造 (query, image) 配对并前向，
    无法复用预计算缓存。分批调用模型并逐批上报进度，避免一次性占用过多显存。

    Args:
        query_text: 查询文本。
        candidates: ``[(相对路径, 图片绝对路径), ...]``。
        on_progress: 进度回调 ``(done, total)``, 每完成一批调用一次。

    Returns:
        ``[(相对路径, 相关性分数), ...]``，按分数降序排列。分数经 sigmoid
        映射到 [0, 1]，与前端阈值语义一致。

    Callers:
        - ``routes/tag_stats:embedding_query``
    """
    model = _get_model()

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
