"""
缩略图生成器 —— AVIF 格式，按需生成，缓存到数据集根 .thumbnails/。

依赖: Pillow (内置 AVIF 编码支持)
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, UnidentifiedImageError

THUMB_DIRNAME = ".thumbnails"
DEFAULT_SIZE = 400  # 长边像素
DEFAULT_QUALITY = 50  # AVIF 质量 (0-100)


def default_workers() -> int:
    """按 CPU 核数选取线程池大小。"""
    return max(1, min(16, (os.cpu_count() or 4)))


class Thumbnailer:
    """AVIF 缩略图管理器。"""

    def __init__(
        self,
        data_dir: Path,
        size: int = DEFAULT_SIZE,
        quality: int = DEFAULT_QUALITY,
        *,
        meta_dir: Path | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.thumb_dir = (meta_dir or data_dir) / THUMB_DIRNAME
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

    def ensure(self, image_rel_path: str, *, src: Path | None = None) -> bool:
        """确保缩略图存在；缺失则生成。src 可指定源文件（如回收站路径）。"""
        if self.exists(image_rel_path):
            return True
        return self.generate(image_rel_path, src=src)

    def generate(self, image_rel_path: str, *, src: Path | None = None) -> bool:
        """
        生成 AVIF 缩略图。已存在则跳过。

        Args:
            image_rel_path: 相对数据集根的图片路径。
            src: 可选源文件路径（如回收站内实际文件）。

        Returns:
            是否成功。源文件缺失或解码失败时返回 False。
        """
        thumb_path = self.get_path(image_rel_path)
        if thumb_path.exists():
            return True

        src_path = src if src is not None else (self.data_dir / image_rel_path)
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
        except (OSError, UnidentifiedImageError, ValueError):
            return False

    def generate_many(
        self,
        image_rel_paths: list[str],
        *,
        workers: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int]:
        """
        并行生成多张缩略图。

        Args:
            image_rel_paths: 相对路径列表。
            workers: 线程数；默认按 CPU 核数自动选取。
            on_progress: 可选回调 ``(done, total)``，每完成一张调用一次。

        Returns:
            ``(成功数, 总数)``。
        """
        paths = list(image_rel_paths)
        total = len(paths)
        if total == 0:
            return 0, 0

        workers = workers or default_workers()
        ok = 0
        done = 0

        def _one(rel: str) -> bool:
            return self.generate(rel)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_one, rel) for rel in paths]
            for fut in as_completed(futs):
                if fut.result():
                    ok += 1
                done += 1
                if on_progress is not None:
                    on_progress(done, total)

        return ok, total
