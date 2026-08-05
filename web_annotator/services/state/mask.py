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
        out_dir: mask 根目录. 
        src_path: 相对数据集根的图片路径(含子目录名, 如 ``main/foo.png``). 

    Returns:
        位于 ``out_dir`` 下的绝对路径. 

    Raises:
        ValueError: 路径越界. 

    引用:
        ``has_mask`` / ``load_mask`` / ``save_mask``；``routes.alpha_paint``. 
    """
    stem_path = resolve_under(out_dir, str(Path(src_path).with_suffix(".png")))
    return stem_path


def has_mask(out_dir: Path, src_path: str) -> bool:
    """判断对应 mask 文件是否存在. 

    引用:
        ``routes.alpha_paint.list_paint_images``. 
    """
    return mask_out_path(out_dir, src_path).is_file()


def load_mask(out_dir: Path, src_path: str, size: tuple[int, int]) -> np.ndarray | None:
    """加载已保存的 alpha mask. 

    Args:
        out_dir: mask 根目录. 
        src_path: 相对原图路径. 
        size: 期望 ``(width, height)``；不一致时返回 ``None``. 

    Returns:
        ``uint8`` 单通道数组；文件不存在或尺寸不符时为 ``None``. 

    Raises:
        OSError: 无法读取文件. 
        UnidentifiedImageError: 文件不是可识别图像. 

    引用:
        ``routes.alpha_paint.get_existing_mask``. 
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
    """保存 mask PNG(白底 RGB + 蒙版 A 通道, 无损). 

    Args:
        out_dir: mask 根目录. 
        src_path: 相对原图路径. 
        alpha: 单通道 ``uint8`` 数组. 

    Returns:
        写入的绝对路径. 

    引用:
        ``routes.alpha_paint.upload_mask``. 
    """
    dest = mask_out_path(out_dir, src_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    h, w = alpha.shape
    white = Image.new("L", (w, h), 255)
    Image.merge("RGBA", (white, white, white, Image.fromarray(alpha, mode="L"))).save(dest, format="PNG")
    return dest
