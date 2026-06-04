#!/usr/bin/env python3
"""
将数据集中所有图片的 alpha 通道分离：
- 原文件原位保存为 RGB PNG（去除 alpha）
- alpha 通道保存到指定的输出目录（灰度 PNG，白=不透明）
- 原图无 alpha 则保存全白 alpha
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path

from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif"}


def iter_images(root: Path, recursive: bool) -> list[Path]:
    """收集目录下所有图片文件"""
    if recursive:
        out: list[Path] = []
        for ext in IMAGE_EXTS:
            out.extend(root.rglob(f"*{ext}"))
            out.extend(root.rglob(f"*{ext.upper()}"))
        return sorted({p.resolve() for p in out if p.is_file()})
    return sorted(
        p
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def atomic_replace_bytes(dest: Path, data: bytes) -> None:
    """原子写入：先写临时文件，再 rename，避免写入中断损坏原文件"""
    tmp = dest.with_name(dest.name + ".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(dest)


def save_png_bytes(im: Image.Image) -> bytes:
    """将 PIL Image 编码为 PNG 字节"""
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def separate_alpha(
    dataset_dir: Path,
    alpha_dir: Path,
    *,
    recursive: bool = False,
    dry_run: bool = False,
) -> None:
    images = iter_images(dataset_dir, recursive)

    if not images:
        print(f"[WARN] 未在 '{dataset_dir}' 中找到图片文件")
        return

    stats = {"processed": 0, "had_alpha": 0, "no_alpha": 0, "skipped": 0, "errors": 0}

    for src in images:
        rel = src.relative_to(dataset_dir)
        alpha_dest = alpha_dir / rel.with_suffix(".png")

        try:
            im = Image.open(src)
        except Exception as e:
            print(f"[ERROR] 无法打开 {src}: {e}")
            stats["errors"] += 1
            continue

        # 判断是否有 alpha 通道
        mode = im.mode
        has_alpha = mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in im.info)

        if has_alpha:
            # 分离 RGB 和 Alpha
            im_rgba = im.convert("RGBA")
            r, g, b, a = im_rgba.split()
            im_rgb = Image.merge("RGB", (r, g, b))
            im_alpha = a  # L 模式灰度图，255=不透明

            stats["had_alpha"] += 1
            tag = "ALPHA"
        else:
            # 无 alpha：原图转 RGB，alpha 用全白
            im_rgb = im.convert("RGB")
            im_alpha = Image.new("L", im_rgb.size, 255)

            stats["no_alpha"] += 1
            tag = "NOALPHA"

        if dry_run:
            print(f"[DRY-RUN] {tag} {src} -> RGB 原位 + alpha -> {alpha_dest}")
            stats["processed"] += 1
            continue

        # 原位保存 RGB PNG
        rgb_bytes = save_png_bytes(im_rgb)
        atomic_replace_bytes(src.with_suffix(".png"), rgb_bytes)

        # 如果原文件后缀不是 .png，删除旧文件
        if src.suffix.lower() != ".png":
            try:
                src.unlink(missing_ok=True)
            except Exception:
                pass

        # 保存 alpha 到输出目录
        alpha_bytes = save_png_bytes(im_alpha)
        alpha_dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace_bytes(alpha_dest, alpha_bytes)

        stats["processed"] += 1
        print(f"[OK] {tag} {rel}")

    print()
    print(f"=== 完成 ===")
    print(f"  处理: {stats['processed']}")
    print(f"  有 alpha: {stats['had_alpha']}")
    print(f"  无 alpha (填充白色): {stats['no_alpha']}")
    print(f"  错误: {stats['errors']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="分离数据集中所有图片的 alpha 通道",
    )
    parser.add_argument(
        "dataset",
        type=Path,
        help="数据集目录路径",
    )
    parser.add_argument(
        "-o", "--alpha-dir",
        type=Path,
        default=None,
        help="alpha 通道输出目录（默认: <dataset>_alpha）",
    )
    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        help="递归处理子目录",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="仅列出将要处理的文件，不实际修改",
    )
    args = parser.parse_args()

    dataset_dir: Path = args.dataset.resolve()
    if not dataset_dir.is_dir():
        parser.error(f"'{dataset_dir}' 不是有效的目录")

    alpha_dir: Path = (
        args.alpha_dir.resolve()
        if args.alpha_dir
        else dataset_dir.parent / f"{dataset_dir.name}_alpha"
    )

    separate_alpha(dataset_dir, alpha_dir, recursive=args.recursive, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
