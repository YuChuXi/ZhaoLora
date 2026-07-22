#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent
REAL_CUGAN_DIR = REPO_ROOT / "Real-CUGAN"
WEIGHT_PATH = REAL_CUGAN_DIR / "weights_v3" / "up2x-latest-no-denoise.pth"

if str(REAL_CUGAN_DIR) not in sys.path:
    sys.path.insert(0, str(REAL_CUGAN_DIR))

from upcunet_v3 import RealWaifuUpScaler  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visualize repeated super-resolution then downscale degradation on a single image."
    )
    parser.add_argument("image", type=Path, help="Input image path")
    parser.add_argument("--iterations", type=int, default=20, help="Max iterations to pre-compute (default: 20)")
    parser.add_argument("--device", default="cuda:0", help='Torch device (default: cuda:0)')
    parser.add_argument("--half", action="store_true", help="Use fp16")
    parser.add_argument("--tile-mode", type=int, default=5, choices=range(6), help="Real-CUGAN tile mode (default: 5)")
    parser.add_argument("--cache-mode", type=int, default=1, choices=range(4), help="Real-CUGAN cache mode (default: 1)")
    parser.add_argument("--alpha", type=float, default=1.0, help="Real-CUGAN alpha (default: 1.0)")
    return parser.parse_args()


def compute_psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    if mse == 0:
        return float("inf")
    return 10.0 * np.log10(255.0**2 / mse)


def upscale_round(img_bgr: np.ndarray, upscaler: RealWaifuUpScaler, tile_mode: int, cache_mode: int, alpha: float) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    rgb = img_bgr[:, :, ::-1].copy()
    result = upscaler(rgb, tile_mode=tile_mode, cache_mode=cache_mode, alpha=alpha)
    result_u8 = result.clip(0, 255).astype("uint8")
    up_bgr = cv2.cvtColor(result_u8, cv2.COLOR_RGB2BGR)
    return cv2.resize(up_bgr, (w, h), interpolation=cv2.INTER_AREA)


def main() -> int:
    args = parse_args()

    if not WEIGHT_PATH.is_file():
        print(f"Weight file not found: {WEIGHT_PATH}", file=sys.stderr)
        return 1

    img_bgr = cv2.imread(str(args.image))
    if img_bgr is None:
        print(f"Cannot read image: {args.image}", file=sys.stderr)
        return 1

    h, w = img_bgr.shape[:2]
    max_dim = 900
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    print(f"Image: {args.image}  size: {img_bgr.shape[1]}x{img_bgr.shape[0]}")
    print(f"Loading Real-CUGAN from: {WEIGHT_PATH}")
    upscaler = RealWaifuUpScaler(2, str(WEIGHT_PATH), args.half, args.device)

    original = img_bgr.copy()
    frames = [original]
    psnrs = [float("inf")]

    print(f"Pre-computing {args.iterations} iterations ...")
    current = original.copy()
    for i in range(1, args.iterations + 1):
        current = upscale_round(current, upscaler, args.tile_mode, args.cache_mode, args.alpha)
        frames.append(current)
        p = compute_psnr(original, current)
        psnrs.append(p)
        print(f"  iter {i:3d}  PSNR={p:.2f} dB")

    print("Done. Use slider to browse iterations. Press ESC to quit.")

    win = "SR Degradation"
    cv2.namedWindow(win)

    def on_trackbar(val: int) -> None:
        idx = min(val, len(frames) - 1)
        frame = frames[idx].copy()
        psnr_val = psnrs[idx]
        psnr_str = f"{psnr_val:.2f}" if psnr_val < 999 else "inf"
        color = (0, 255, 0) if idx == 0 else (0, 200, 255) if psnr_val > 30 else (0, 0, 255)

        cv2.putText(frame, f"Iter: {idx}", (12, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 3)
        cv2.putText(frame, f"Iter: {idx}", (12, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 1)
        cv2.putText(frame, f"PSNR: {psnr_str} dB", (12, 74), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 3)
        cv2.putText(frame, f"PSNR: {psnr_str} dB", (12, 74), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 1)
        cv2.imshow(win, frame)

    cv2.createTrackbar("Iteration", win, 0, args.iterations, on_trackbar)
    on_trackbar(0)

    while True:
        if cv2.waitKey(1) & 0xFF == 27:
            break

    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())