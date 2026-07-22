"""
缩略图生成器 —— AVIF 格式，按需生成，缓存到 .thumbnails/。

依赖: Pillow (内置 AVIF 编码支持)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

THUMB_DIRNAME = ".thumbnails"
DEFAULT_SIZE = 400  # 长边像素
DEFAULT_QUALITY = 50  # AVIF 质量 (0-100)


class Thumbnailer:
    """AVIF 缩略图管理器。"""

    def __init__(self, data_dir: Path, size: int = DEFAULT_SIZE, quality: int = DEFAULT_QUALITY) -> None:
        self.data_dir = data_dir
        self.thumb_dir = data_dir / THUMB_DIRNAME
        self.size = size
        self.quality = quality
        self.thumb_dir.mkdir(parents=True, exist_ok=True)

    def hash_for(self, image_rel_path: str) -> str:
        """计算图片的缩略图 hash 文件名（不含后缀）。"""
        return hashlib.md5(image_rel_path.encode()).hexdigest()[:12]

    def get_path(self, image_rel_path: str) -> Path:
        """获取缩略图缓存路径。"""
        return self.thumb_dir / f"{self.hash_for(image_rel_path)}.avif"

    def exists(self, image_rel_path: str) -> bool:
        """缩略图是否已缓存。"""
        return self.get_path(image_rel_path).exists()

    def generate(self, image_rel_path: str) -> bool:
        """
        生成 AVIF 缩略图。已存在则跳过。返回是否成功。

        失败场景（损坏图片等）返回 False，不抛异常。
        """
        thumb_path = self.get_path(image_rel_path)
        if thumb_path.exists():
            return True

        src_path = self.data_dir / image_rel_path
        if not src_path.exists():
            return False

        try:
            img = Image.open(src_path)
            img = img.convert("RGB")
            w, h = img.size
            if max(w, h) > self.size:
                ratio = self.size / max(w, h)
                new_size = (int(w * ratio), int(h * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            img.save(thumb_path, format="AVIF", quality=self.quality)
            return True
        except Exception:
            return False
