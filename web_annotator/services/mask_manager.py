"""
Mask 文件管理 —— alpha mask PNG，与 alpha_paint_tk.py 完全兼容。
存到独立的 out_dir，文件名与原图对应，后缀 .png。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


def mask_out_path(out_dir: Path, src_path: str) -> Path:
    """mask 输出路径: out_dir/<相对路径>.png，与 alpha_paint_tk.py 一致。"""
    return out_dir / Path(src_path).with_suffix(".png")


def has_mask(out_dir: Path, src_path: str) -> bool:
    return mask_out_path(out_dir, src_path).is_file()


def load_mask(out_dir: Path, src_path: str, size: tuple[int, int]) -> np.ndarray | None:
    """加载已保存的 alpha mask，尺寸不匹配则返回 None。"""
    p = mask_out_path(out_dir, src_path)
    if not p.is_file():
        return None
    try:
        im = Image.open(p)
        if im.size != size:
            return None
        if im.mode in ("L", "LA"):
            return np.array(im.getchannel("L"), dtype=np.uint8, copy=True)
        rgba = im.convert("RGBA")
        return np.array(rgba.getchannel("A"), dtype=np.uint8, copy=True)
    except Exception:
        return None


def save_mask(out_dir: Path, src_path: str, alpha: np.ndarray, src_image_path: Path,
               save_format: str = "alpha_bw") -> Path:
    """保存 mask PNG，与 alpha_paint_tk.py 完全一致。
       alpha_bw: 纯灰度 alpha
       rgba: 白底原图 RGB + alpha
    """
    dest = mask_out_path(out_dir, src_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if save_format == "alpha_bw":
        Image.fromarray(alpha, mode="L").save(dest, format="PNG")
    else:
        im = Image.open(src_image_path).convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        if alpha.shape[::-1] != bg.size:
            raise ValueError(f"alpha shape {alpha.shape} != image size {bg.size}")
        r, g, b = bg.split()
        Image.merge("RGBA", (r, g, b, Image.fromarray(alpha, mode="L"))).save(dest, format="PNG")
    return dest
