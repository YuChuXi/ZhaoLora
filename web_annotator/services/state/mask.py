"""
Mask 文件管理 —— alpha mask PNG.
存到独立的 mask 根目录, 路径与原图相对数据集根的路径对应, 后缀 .png.
例如 main 图 ``main/foo.png`` → ``mask/main/foo.png``, 部位图 ``head/foo.png`` → ``mask/head/foo.png``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from services.core.work_dir import resolve_under


def mask_out_path(out_dir: Path, src_path: str) -> Path:
    """计算 mask 输出路径: ``out_dir/<相对数据集根路径>.png``.

    Args:
        out_dir (Path): mask 根目录.
        src_path (str): 相对数据集根的图片路径(含子目录名, 如 ``main/foo.png``).

    Returns:
        Path: 位于 ``out_dir`` 下的绝对路径.

    Raises:
        ValueError: 路径越界.

    Callers:
        - ``services/state/mask.py:has_mask``
        - ``services/state/mask.py:load_mask``
        - ``services/state/mask.py:save_mask``
        - ``routes/alpha_paint.py:serve_mask_preview``
    """
    stem_path = resolve_under(out_dir, str(Path(src_path).with_suffix(".png")))
    return stem_path


def has_mask(out_dir: Path, src_path: str) -> bool:
    """判断对应 mask 文件是否存在.

    Args:
        out_dir (Path): mask 根目录.
        src_path (str): 相对数据集根的图片路径.

    Returns:
        bool: mask 文件存在则为 True.

    Callers:
        - ``routes/alpha_paint.py:list_paint_images``
        - ``services/ready.py:subdir_mask_stats``
    """
    return mask_out_path(out_dir, src_path).is_file()


def load_mask(out_dir: Path, src_path: str, size: tuple[int, int]) -> np.ndarray | None:
    """加载已保存的 alpha mask.

    Args:
        out_dir (Path): mask 根目录.
        src_path (str): 相对原图路径.
        size (tuple[int, int]): 期望 ``(width, height)``; 不一致时返回 ``None``.

    Returns:
        np.ndarray | None: ``uint8`` 单通道数组; 文件不存在或尺寸不符时为 ``None``.

    Raises:
        OSError: 无法读取文件.
        UnidentifiedImageError: 文件不是可识别图像.

    Callers:
        - ``routes/alpha_paint.py:get_existing_mask``
        - ``routes/alpha_paint.py:serve_mask_preview``
    """
    p = mask_out_path(out_dir, src_path)
    if not p.is_file():
        return None
    im = Image.open(p)
    if im.size != size:
        return None
    if im.mode in ("L", "LA"):
        return np.array(im.getchannel("L"), dtype=np.uint8, copy=True)
    rgba = im.convert("RGBA")
    return np.array(rgba.getchannel("A"), dtype=np.uint8, copy=True)


def save_mask(out_dir: Path, src_path: str, alpha: np.ndarray) -> Path:
    """保存蒙板为灰度 PNG(单通道 L, 像素值即 0-255 蒙板, 无损).

    Args:
        out_dir (Path): mask 根目录.
        src_path (str): 相对原图路径.
        alpha (np.ndarray): 单通道 ``uint8`` 数组.

    Returns:
        Path: 写入的绝对路径.

    Callers:
        - ``routes/alpha_paint.py:upload_mask``
    """
    dest = mask_out_path(out_dir, src_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(alpha, mode="L").save(dest, format="PNG")
    return dest
