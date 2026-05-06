#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path


def iter_files(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        return sorted(p for p in root.rglob("*") if p.is_file())
    return sorted(p for p in root.iterdir() if p.is_file())


def atomic_replace_bytes(dest: Path, data: bytes) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(dest)


def jpg_to_png(src: Path, *, keep_jpg: bool) -> Path:
    from PIL import Image  # type: ignore

    im = Image.open(src)
    im = im.convert("RGB")
    out = src.with_suffix(".png")
    if out.exists():
        stem = out.stem
        i = 1
        while True:
            cand = out.with_name(f"{stem}__{i}.png")
            if not cand.exists():
                out = cand
                break
            i += 1
    buf = bytearray()
    import io

    bio = io.BytesIO()
    im.save(bio, format="PNG", optimize=True)
    buf.extend(bio.getvalue())
    atomic_replace_bytes(out, bytes(buf))
    if not keep_jpg:
        src.unlink()
    return out


def ensure_png_has_alpha(path: Path, *, rewrite_even_if_rgba: bool) -> bool:
    """
    若 PNG 无 alpha 通道则补全为 RGBA（全不透明）；若为调色板/灰度带透明信息，也转为 RGBA。
    返回 True 表示写回了文件。
    """
    from PIL import Image  # type: ignore

    im = Image.open(path)
    need = False

    # 统一将可能含透明信息的模式转为 RGBA，再判断是否需要落盘
    if im.mode in ("RGBA", "RGBa"):
        rgba = im
        if rewrite_even_if_rgba:
            need = True
        else:
            need = False
    elif im.mode in ("LA",):
        rgba = im.convert("RGBA")
        need = True
    elif im.mode == "P":
        if im.info.get("transparency") is not None or "transparency" in im.info:
            rgba = im.convert("RGBA")
            need = True
        else:
            rgba = im.convert("RGBA")
            need = True
    elif im.mode in ("RGB", "L", "CMYK"):
        rgba = im.convert("RGBA")
        need = True
    else:
        rgba = im.convert("RGBA")
        need = True

    if not need:
        return False

    import io

    bio = io.BytesIO()
    rgba.save(bio, format="PNG", optimize=True)
    atomic_replace_bytes(path, bio.getvalue())
    return True


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="目录内 jpg->png，并为 png 补全 alpha 通道")
    ap.add_argument("dir", type=Path, help="目标目录")
    ap.add_argument("-r", "--recursive", action="store_true", help="递归子目录")
    ap.add_argument("--keep-jpg", action="store_true", help="转换后保留原 jpg/jpeg")
    ap.add_argument(
        "--rewrite-rgba",
        action="store_true",
        help="即使 PNG 已是 RGBA 也重写一次（默认不重写）",
    )
    args = ap.parse_args(argv)

    root = args.dir.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"目录不存在: {root}")

    try:
        import PIL  # noqa: F401  # type: ignore
    except ModuleNotFoundError as e:
        raise SystemExit("缺少 Pillow：请先安装 `pip install pillow`") from e

    jpg_n = 0
    png_fixed = 0
    skipped = 0

    for p in iter_files(root, args.recursive):
        suf = p.suffix.lower()
        if suf in (".jpg", ".jpeg"):
            out = jpg_to_png(p, keep_jpg=args.keep_jpg)
            jpg_n += 1
            print(f"jpg->png: {p.relative_to(root)} -> {out.relative_to(root)}", flush=True)
            continue
        if suf == ".png":
            changed = ensure_png_has_alpha(p, rewrite_even_if_rgba=args.rewrite_rgba)
            if changed:
                png_fixed += 1
                print(f"png+alpha: {p.relative_to(root)}", flush=True)
            else:
                skipped += 1
            continue

    print(f"完成: jpg转换 {jpg_n} | png补alpha {png_fixed} | png跳过 {skipped}", flush=True)


if __name__ == "__main__":
    main()

