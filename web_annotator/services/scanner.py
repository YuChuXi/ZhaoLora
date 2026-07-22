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


def scan_images(data_dir: Path) -> list[dict]:
    """
    扫描 data_dir 下所有图片文件及其关联文件。

    返回列表，每项:
      {
        "path": str,        # 相对于 data_dir 的路径
        "stem": str,        # 无后缀文件名
        "suffix": str,      # 后缀（含点）
        "size": int,        # 文件字节数
        "siblings": [str],  # 同 stem 的非图片文件路径列表
      }
    """
    # 建立 stem -> 文件列表 的索引
    stem_map: dict[str, list[Path]] = {}
    for f in data_dir.iterdir():
        if not f.is_file():
            continue
        stem_map.setdefault(f.stem, []).append(f)

    result: list[dict] = []
    for stem, files in sorted(stem_map.items(), key=lambda x: _natural_key(x[0])):
        # 找图片文件
        img_file = None
        siblings: list[str] = []
        for f in files:
            if f.suffix.lower() in IMAGE_EXTS:
                img_file = f
            else:
                siblings.append(str(f.relative_to(data_dir)))

        if img_file is None:
            continue  # 只有附属文件没有图，跳过

        result.append({
            "path": str(img_file.relative_to(data_dir)),
            "stem": img_file.stem,
            "suffix": img_file.suffix.lower(),
            "size": img_file.stat().st_size,
            "siblings": sorted(siblings),
        })

    return result
