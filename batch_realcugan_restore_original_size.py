#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parent
REAL_CUGAN_DIR = REPO_ROOT / "Real-CUGAN"
WEIGHT_PATH = REAL_CUGAN_DIR / "weights_v3" / "up2x-latest-no-denoise.pth"

if str(REAL_CUGAN_DIR) not in sys.path:
    sys.path.insert(0, str(REAL_CUGAN_DIR))

from upcunet_v3 import RealWaifuUpScaler  # noqa: E402


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batch process images with Real-CUGAN 2x no-denoise model, "
            "then resize back to original resolution using nearest-neighbor."
        )
    )
    parser.add_argument("input_dir", type=Path, help="Input image directory")
    parser.add_argument("output_dir", type=Path, help="Output image directory")
    parser.add_argument(
        "--device",
        default="cuda:0",
        help='Torch device, e.g. "cuda:0" or "cpu" (default: cuda:0)',
    )
    parser.add_argument(
        "--half",
        action="store_true",
        help="Use fp16. Recommended on newer NVIDIA GPUs.",
    )
    parser.add_argument(
        "--tile-mode",
        type=int,
        default=5,
        choices=[0, 1, 2, 3, 4, 5],
        help="Real-CUGAN tile mode (default: 5)",
    )
    parser.add_argument(
        "--cache-mode",
        type=int,
        default=1,
        choices=[0, 1, 2, 3],
        help="Real-CUGAN cache mode (default: 1)",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=1.0,
        help="Real-CUGAN alpha (default: 1.0)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively process images in subdirectories",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output files if they already exist",
    )
    parser.add_argument(
        "--suffix",
        default="_cugan_nearest",
        help='Suffix appended before file extension (default: "_cugan_nearest")',
    )
    return parser.parse_args()


def iter_images(input_dir: Path, recursive: bool) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    files = [p for p in input_dir.glob(pattern) if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files)


def build_output_path(
    src_path: Path,
    input_dir: Path,
    output_dir: Path,
    suffix: str,
) -> Path:
    rel_path = src_path.relative_to(input_dir)
    return output_dir / rel_path.parent / f"{src_path.stem}{suffix}.png"


def process_one(
    src_path: Path,
    dst_path: Path,
    upscaler: RealWaifuUpScaler,
    tile_mode: int,
    cache_mode: int,
    alpha: float,
) -> None:
    image_bgr = cv2.imread(str(src_path), cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise RuntimeError(f"Failed to read image: {src_path}")

    orig_h, orig_w = image_bgr.shape[:2]
    image_rgb = image_bgr[:, :, ::-1].copy()

    result_rgb = upscaler(image_rgb, tile_mode=tile_mode, cache_mode=cache_mode, alpha=alpha)
    result_rgb_u8 = (result_rgb.clip(0, 255)).astype("uint8")

    # restored = Image.fromarray(result_rgb_u8).resize(
    #     (orig_w, orig_h), Image.Resampling.NEAREST
    # )
    restored_bgr = cv2.cvtColor(np.asarray(result_rgb_u8), cv2.COLOR_RGB2BGR)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(dst_path), restored_bgr)
    if not ok:
        raise RuntimeError(f"Failed to write image: {dst_path}")


def main() -> int:
    args = parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()

    if not input_dir.is_dir():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 1
    if not WEIGHT_PATH.is_file():
        print(f"Weight file does not exist: {WEIGHT_PATH}", file=sys.stderr)
        return 1
    if args.device == "cpu" and args.half:
        print("--half is not supported with cpu; please remove --half.", file=sys.stderr)
        return 1

    image_paths = iter_images(input_dir, args.recursive)
    if not image_paths:
        print(f"No images found in: {input_dir}")
        return 0

    print(f"Loading model from: {WEIGHT_PATH}")
    print(f"Device: {args.device}, half: {args.half}")
    upscaler = RealWaifuUpScaler(2, str(WEIGHT_PATH), args.half, args.device)

    processed = 0
    skipped = 0
    failed = 0

    for index, src_path in enumerate(image_paths, start=1):
        dst_path = build_output_path(src_path, input_dir, output_dir, args.suffix)
        if dst_path.exists() and not args.overwrite:
            skipped += 1
            print(f"[{index}/{len(image_paths)}] skip {src_path}")
            continue

        try:
            process_one(
                src_path=src_path,
                dst_path=dst_path,
                upscaler=upscaler,
                tile_mode=args.tile_mode,
                cache_mode=args.cache_mode,
                alpha=args.alpha,
            )
            processed += 1
            print(f"[{index}/{len(image_paths)}] done {src_path} -> {dst_path}")
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(image_paths)}] fail {src_path}: {exc}", file=sys.stderr)

    print(
        f"Finished. processed={processed}, skipped={skipped}, failed={failed}, total={len(image_paths)}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
