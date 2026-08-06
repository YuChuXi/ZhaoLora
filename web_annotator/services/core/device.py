"""CUDA 设备检测工具, 供各分析模块共享使用. """

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # torch 惰性导入, 仅用于类型注解
    import torch

_TORCH_DEVICE: torch.device | None = None
_DEVICE_CHECKED = False


def cuda_device() -> torch.device | None:
    """返回可用的 CUDA 设备对象, 无 CUDA 或未安装 torch 时返回 ``None``.

    惰性检测并缓存, 供 FFT 特征提取与两两相似度对比等计算复用. 返回
    ``None`` 时调用方应回退到 CPU (numpy) 实现.

    Returns:
        torch.device | None: CUDA 设备对象, 不可用时为 ``None``.

    Callers:
        - `analysis/dedupe.py:extract_fft_features`
        - `analysis/dedupe.py:pairwise_values`
        - `analysis/dedupe.py:find_duplicate_groups`
    """
    global _TORCH_DEVICE, _DEVICE_CHECKED
    if not _DEVICE_CHECKED:
        try:
            import torch
            _TORCH_DEVICE = torch.device("cuda") if torch.cuda.is_available() else None
        except ImportError:
            _TORCH_DEVICE = None
        _DEVICE_CHECKED = True
    return _TORCH_DEVICE