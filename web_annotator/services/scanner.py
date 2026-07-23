"""
图片扫描器 —— 在数据目录中发现图片及其关联文件。

设计约束：
  - 不递归子目录（数据集目录内是扁平结构）
  - 识别图片后缀: .png .jpg .jpeg .webp .bmp .gif
  - 发现同 stem 的关联文件 (.txt, .npz 等) 作为 siblings
"""

from __future__ import annotations

import re
from pathlib import Path

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})


def _natural_key(s: str) -> tuple:
    """自然排序键: 'img2_abc10' → ('img', 2, '_abc', 10)."""
    parts = re.split(r"(\d+)", s)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def scan_images(data_dir: Path, *, path_prefix: str = "") -> list[dict]:
    """
    扫描 data_dir 下所有图片文件及其关联文件。

    path_prefix 非空时，返回的 path / siblings 会加上此前缀（相对数据集根），
    例如 path_prefix=\"main\" → path=\"main/foo.png\"。
    """
    prefix = (path_prefix or "").replace("\\", "/").strip("/")
    stem_map: dict[str, list[Path]] = {}
    if not data_dir.is_dir():
        return []

    for f in data_dir.iterdir():
        if not f.is_file():
            continue
        stem_map.setdefault(f.stem, []).append(f)

    result: list[dict] = []
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
        result.append({
            "path": rel,
            "stem": img_file.stem,
            "suffix": img_file.suffix.lower(),
            "size": img_file.stat().st_size,
            "siblings": sorted(siblings),
        })

    return result
