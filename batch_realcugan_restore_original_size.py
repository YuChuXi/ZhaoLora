#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parent
REAL_CUGAN_DIR = REPO_ROOT / "Real-CUGAN"
WEIGHT_PATH = REAL_CUGAN_DIR / "weights_v3" / "up2x-latest-no-denoise.pth"

if str(REAL_CUGAN_DIR) not in sys.path:
    sys.path.insert(0, str(REAL_CUGAN_DIR))

from upcunet_v3 import RealWaifuUpScaler  # noqa: E402


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
REMOVE_TAGS = {"jpeg artifacts"}


@dataclass
class Task:
    img_src: Path
    img_dst: Path
    mask_src: Path | None = None
    mask_dst: Path | None = None
    txt_src: Path | None = None
    txt_dst: Path | None = None

    @property
    def all_dst(self) -> list[Path]:
        return [p for p in (self.img_dst, self.mask_dst, self.txt_dst) if p is not None]

    @property
    def companion_labels(self) -> list[str]:
        parts = []
        if self.mask_src:
            parts.append("mask")
        if self.txt_src:
            parts.append("txt")
        return parts


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
    parser.add_argument(
        "--mask-input-dir",
        type=Path,
        default=None,
        help="Mask input directory. Masks are matched by stem name with images.",
    )
    parser.add_argument(
        "--mask-output-dir",
        type=Path,
        default=None,
        help="Mask output directory. Upscaled masks are saved here (nearest-neighbor 2x).",
    )
    return parser.parse_args()


def iter_images(input_dir: Path, recursive: bool) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return sorted(
        p for p in input_dir.glob(pattern) if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def build_output_path(src_path: Path, input_dir: Path, output_dir: Path, suffix: str, ext: str = ".png") -> Path:
    rel_path = src_path.relative_to(input_dir)
    return output_dir / rel_path.parent / f"{src_path.stem}{suffix}{ext}"


def find_companion_image(directory: Path, stem: str) -> Path | None:
    for ext in IMAGE_EXTS:
        candidate = directory / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    return None


def clean_caption(text: str) -> str:
    parts = text.split("|||")
    cleaned = []
    for part in parts:
        tags = [t.strip() for t in part.split(",")]
        tags = [t for t in tags if t.lower() not in REMOVE_TAGS]
        cleaned.append(", ".join(tags))
    return " ||| ".join(cleaned)


def resolve_task(
    src_path: Path,
    input_dir: Path,
    output_dir: Path,
    suffix: str,
    mask_input_dir: Path | None,
    mask_output_dir: Path | None,
) -> Task:
    img_dst = build_output_path(src_path, input_dir, output_dir, suffix)

    mask_src = find_companion_image(mask_input_dir, src_path.stem) if mask_input_dir else None
    mask_dst = (
        build_output_path(src_path, input_dir, mask_output_dir, suffix)
        if mask_src and mask_output_dir
        else None
    )

    txt_src = src_path.with_suffix(".txt")
    if not txt_src.is_file():
        txt_src = None
    txt_dst = (
        build_output_path(src_path, input_dir, output_dir, suffix, ext=".txt")
        if txt_src
        else None
    )

    return Task(
        img_src=src_path, img_dst=img_dst,
        mask_src=mask_src, mask_dst=mask_dst,
        txt_src=txt_src, txt_dst=txt_dst,
    )


def process_one(task: Task, upscaler: RealWaifuUpScaler, tile_mode: int, cache_mode: int, alpha: float) -> None:
    image_bgr = cv2.imread(str(task.img_src), cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise RuntimeError(f"Failed to read image: {task.img_src}")

    orig_h, orig_w = image_bgr.shape[:2]
    image_rgb = image_bgr[:, :, ::-1].copy()

    result_rgb = upscaler(image_rgb, tile_mode=tile_mode, cache_mode=cache_mode, alpha=alpha)
    result_rgb_u8 = (result_rgb.clip(0, 255)).astype("uint8")
    restored_bgr = cv2.cvtColor(np.asarray(result_rgb_u8), cv2.COLOR_RGB2BGR)

    task.img_dst.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(task.img_dst), restored_bgr):
        raise RuntimeError(f"Failed to write image: {task.img_dst}")

    if task.mask_src and task.mask_dst:
        mask = cv2.imread(str(task.mask_src), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            raise RuntimeError(f"Failed to read mask: {task.mask_src}")
        mask_up = cv2.resize(mask, (orig_w * 2, orig_h * 2), interpolation=cv2.INTER_NEAREST)
        task.mask_dst.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(task.mask_dst), mask_up):
            raise RuntimeError(f"Failed to write mask: {task.mask_dst}")

    if task.txt_src and task.txt_dst:
        cleaned = clean_caption(task.txt_src.read_text(encoding="utf-8"))
        task.txt_dst.parent.mkdir(parents=True, exist_ok=True)
        task.txt_dst.write_text(cleaned, encoding="utf-8")


def main() -> int:
    args = parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    mask_input_dir = args.mask_input_dir.resolve() if args.mask_input_dir else None
    mask_output_dir = args.mask_output_dir.resolve() if args.mask_output_dir else None

    if not input_dir.is_dir():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 1
    if not WEIGHT_PATH.is_file():
        print(f"Weight file does not exist: {WEIGHT_PATH}", file=sys.stderr)
        return 1
    if args.device == "cpu" and args.half:
        print("--half is not supported with cpu; please remove --half.", file=sys.stderr)
        return 1
    if (mask_input_dir is None) != (mask_output_dir is None):
        print("--mask-input-dir and --mask-output-dir must be specified together.", file=sys.stderr)
        return 1
    if mask_input_dir is not None and not mask_input_dir.is_dir():
        print(f"Mask input directory does not exist: {mask_input_dir}", file=sys.stderr)
        return 1

    image_paths = iter_images(input_dir, args.recursive)
    if not image_paths:
        print(f"No images found in: {input_dir}")
        return 0

    print(f"Loading model from: {WEIGHT_PATH}")
    print(f"Device: {args.device}, half: {args.half}")
    if mask_input_dir:
        print(f"Mask input: {mask_input_dir}")
        print(f"Mask output: {mask_output_dir}")
    upscaler = RealWaifuUpScaler(2, str(WEIGHT_PATH), args.half, args.device)

    processed = 0
    skipped = 0
    failed = 0
    mask_missing = 0

    for index, src_path in enumerate(image_paths, start=1):
        task = resolve_task(src_path, input_dir, output_dir, args.suffix, mask_input_dir, mask_output_dir)

        if task.mask_src is None and mask_input_dir is not None:
            mask_missing += 1
            print(f"[{index}/{len(image_paths)}] warn no mask for {src_path.name}", file=sys.stderr)

        if not args.overwrite and all(p.exists() for p in task.all_dst):
            skipped += 1
            print(f"[{index}/{len(image_paths)}] skip {src_path}")
            continue

        try:
            process_one(task, upscaler, args.tile_mode, args.cache_mode, args.alpha)
            processed += 1
            labels = task.companion_labels
            extra = f" +{'+'.join(labels)}" if labels else ""
            print(f"[{index}/{len(image_paths)}] done {src_path} -> {task.img_dst}{extra}")
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(image_paths)}] fail {src_path}: {exc}", file=sys.stderr)

    print(f"Finished. processed={processed}, skipped={skipped}, failed={failed}, mask_missing={mask_missing}, total={len(image_paths)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())