"""缩略图生成器 —— AVIF 格式, 按需生成, 缓存到数据集根 .thumbnails/. 

依赖: Pillow (需系统安装 libavif 或 pillow-avif-plugin)
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, UnidentifiedImageError

THUMB_DIRNAME = ".thumbnails"
DEFAULT_SIZE = 2560  # 长边像素(与 server --thumbnail-size 默认一致)
DEFAULT_QUALITY = 50  # AVIF 质量 (0-100)


def default_workers() -> int:
    """按 CPU 核数选取线程池大小, 范围 [1, 16]. 

    Returns:
        int: 建议的线程数. 

    Callers:
        - ``Thumbnailer.generate_many``
    """
    return max(1, min(16, (os.cpu_count() or 4)))


class Thumbnailer:
    """AVIF 缩略图管理器. 

    Callers:
        - `dataset_runtime.py:DatasetRegistry.ensure`
    """

    def __init__(
        self,
        data_dir: Path,
        size: int = DEFAULT_SIZE,
        quality: int = DEFAULT_QUALITY,
        *,
        meta_dir: Path | None = None,
    ) -> None:
        """绑定缩略图目录与生成参数. 

        Args:
            data_dir: 数据集根目录. 
            size: 缩略图长边像素数. 
            quality: AVIF 质量参数 (0-100). 
            meta_dir: 缩略图缓存目录；默认与 ``data_dir`` 相同. 

        Callers:
            - ``services/dataset/runtime.py:DatasetRegistry.ensure``
        """
        self.data_dir = data_dir
        self.thumb_dir = (meta_dir or data_dir) / THUMB_DIRNAME
        self.size = int(size)
        self.quality = quality
        self.thumb_dir.mkdir(parents=True, exist_ok=True)

    def fill_missing(self) -> int:
        """扫描 main/ 与 mask/, 为所有缺失缩略图的图片批量生成. 

        Returns:
            int: 本次成功生成的缩略图数量. 

        Callers:
            - ``services/dataset/runtime.py:_submit_thumb_fill``
        """
        rel_paths: list[str] = []
        for sub in ("main", "mask"):
            d = self.data_dir / sub
            if not d.is_dir():
                continue
            for img in d.iterdir():
                if not img.is_file() or img.suffix.lower() in (".txt", ".json"):
                    continue
                rel_paths.append(f"{sub}/{img.name}")
        if not rel_paths:
            return 0
        ok, _ = self.generate_many(rel_paths)
        return ok

    def hash_for(self, image_rel_path: str) -> str:
        """计算缩略图文件名 hash(不含后缀), 计入长边尺寸避免缓存冲突. 

        Args:
            image_rel_path: 相对数据集根的图片路径. 

        Returns:
            str: 12 位十六进制 hash 字符串. 

        Callers:
            - ``Thumbnailer.get_path``
            - ``server.py:dataset_thumb``
        """
        key = f"{image_rel_path}\0s{self.size}".encode()
        return hashlib.md5(key).hexdigest()[:12]

    def find_rel_path(self, thumb_filename: str) -> str | None:
        """根据缩略图文件名(含 .avif 后缀)反查原图相对路径. 

        Args:
            thumb_filename: 缩略图文件名(如 ``abc123.avif``). 

        Returns:
            str | None: 原图相对路径, 未找到时返回 None. 

        Callers:
            - ``server.py:dataset_thumb``
        """
        target_hash = Path(thumb_filename).stem
        for sub in ("main", "mask"):
            d = self.data_dir / sub
            if not d.is_dir():
                continue
            for img in d.iterdir():
                if not img.is_file() or img.suffix.lower() in (".txt", ".json"):
                    continue
                rel = f"{sub}/{img.name}"
                if self.hash_for(rel) == target_hash:
                    return rel
        return None

    def get_path(self, image_rel_path: str) -> Path:
        """获取缩略图在磁盘缓存中的绝对路径. 

        Args:
            image_rel_path: 相对数据集根的图片路径. 

        Returns:
            Path: 缩略图文件路径(``.thumbnails/<hash>.avif``). 

        Callers:
            - ``Thumbnailer.exists``
            - ``Thumbnailer.generate``
        """
        return self.thumb_dir / f"{self.hash_for(image_rel_path)}.avif"

    def exists(self, image_rel_path: str) -> bool:
        """检查缩略图是否已缓存. 

        Args:
            image_rel_path: 相对数据集根的图片路径. 

        Returns:
            bool: 缓存文件存在则为 True. 

        Callers:
            - ``Thumbnailer.ensure``
        """
        return self.get_path(image_rel_path).exists()

    def ensure(self, image_rel_path: str, *, src: Path | None = None) -> bool:
        """确保缩略图存在；缺失则生成. 

        Args:
            image_rel_path: 相对数据集根的图片路径. 
            src: 可选源文件路径(如回收站路径). 

        Returns:
            bool: 是否已存在或生成成功. 

        Callers:
            - ``services/booru.py``: 下载后确保缩略图存在
        """
        if self.exists(image_rel_path):
            return True
        return self.generate(image_rel_path, src=src)

    def generate(self, image_rel_path: str, *, src: Path | None = None) -> bool:
        """生成单张 AVIF 缩略图. 已存在则跳过. 

        Args:
            image_rel_path: 相对数据集根的图片路径. 
            src: 可选源文件路径. 

        Returns:
            bool: 是否成功. 源文件缺失或解码失败时返回 False. 

        Callers:
            - `thumbnailer.py:Thumbnailer.ensure`
            - `thumbnailer.py:Thumbnailer.fill_missing`
            - `thumbnailer.py:Thumbnailer.generate_many`
            - `server.py:dataset_thumb`
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
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            img.save(thumb_path, format="AVIF", quality=self.quality)
            return True
        except (KeyError, OSError, UnidentifiedImageError, ValueError):
            return False

    def generate_many(
        self,
        image_rel_paths: list[str],
        *,
        workers: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int]:
        """并行生成多张缩略图. 

        Args:
            image_rel_paths: 相对路径列表. 
            workers: 线程数；默认按 CPU 核数自动选取. 
            on_progress: 可选回调 ``(done, total)``, 每完成一张调用一次. 

        Returns:
            ``(成功数, 总数)``. 

        Callers:
            - `thumbnailer.py:Thumbnailer.fill_missing`
        """
        paths = list(image_rel_paths)
        total = len(paths)
        if total == 0:
            return 0, 0

        workers = workers or default_workers()
        ok = 0
        done = 0

        def _one(rel: str) -> bool:
            """生成单张缩略图. 

            Callers:
                - `generate_many`
            """
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
