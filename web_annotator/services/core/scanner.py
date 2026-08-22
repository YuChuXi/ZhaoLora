"""图片扫描器: 在数据目录中发现图片及其旁路文件. 

约束: 
  - 不递归子目录(目录内扁平)
  - 识别 ``IMAGE_EXTS``
  - 同 stem 的非图像文件记入 ``siblings``
"""

from __future__ import annotations

import re
from pathlib import Path

from services.models import ScannedImage

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})


def collect_images(dirs: list[Path]) -> list[Path]:
    """收集多个目录下所有图片文件的路径. 

    目录内扁平扫描(不递归), 按文件名排序. 

    Args:
        dirs: 要扫描的目录列表. 

    Returns:
        list[Path]: 图片绝对路径列表. 

    Callers:
        - ``services/analysis/scoring.py:_run_scoring``
        - ``services/analysis/wd14.py:run_batch``
    """
    items: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                items.append(p)
    return items


def _natural_key(s: str) -> tuple:
    """自然排序键: ``img2_abc10`` → ``('img', 2, '_abc', 10)``.

    Args:
        s: 待解析字符串.

    Returns:
        tuple: 用于自然排序的键(数字段转为 int).
    """
    parts = re.split(r"(\d+)", s)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def scan_images(data_dir: Path, *, path_prefix: str = "") -> list[ScannedImage]:
    """扫描 ``data_dir`` 下图片及同 stem 旁路文件. 

    Args:
        data_dir: 待扫描目录. 
        path_prefix: 非空时加在 ``path`` / ``siblings`` 前(相对数据集根), 
            例如 ``main`` → ``main/foo.png``. 

    Returns:
        按自然序排列的 ``ScannedImage`` 列表.

    Callers:
        - ``routes/images.py:list_images`` / `similar_images`
        - ``routes/annotations.py:select_all`` / `get_state`
        - ``routes/crop.py:list_crop_images``
        - ``routes/alpha_paint.py:list_paint_images``
        - ``routes/dedupe.py:apply_delete``
        - ``routes/tasks.py:list_tasks``
        - ``routes/tag_stats.py:check`` / `embedding_query` / `concept_query`
        - ``services/ready.py:subdir_mask_stats``
        - ``services/state/archiver.py:archive_task``
    """
    prefix = (path_prefix or "").replace("\\", "/").strip("/")
    stem_map: dict[str, list[Path]] = {}
    if not data_dir.is_dir():
        return []

    for f in data_dir.iterdir():
        if not f.is_file():
            continue
        stem_map.setdefault(f.stem, []).append(f)

    result: list[ScannedImage] = []
    for stem, files in sorted(stem_map.items(), key=lambda x: _natural_key(x[0])):
        img_file = None
        siblings: list[str] = []
        for f in files:
            if f.suffix.lower() in IMAGE_EXTS:
                img_file = f
            else:
                name = f.name
                siblings.append(f"{prefix}/{name}" if prefix else name)

        if img_file is None:
            continue

        name = img_file.name
        rel = f"{prefix}/{name}" if prefix else name
        result.append(
            ScannedImage(
                path=rel,
                stem=img_file.stem,
                suffix=img_file.suffix.lower(),
                size=img_file.stat().st_size,
                siblings=sorted(siblings),
            )
        )

    return result
