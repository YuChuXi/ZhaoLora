#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:
    print("需要 opencv-python：pip install opencv-python", file=sys.stderr)
    sys.exit(1)


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def iter_image_paths(root: Path, recursive: bool) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    if recursive:
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.append(p)
    else:
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.append(p)
    out.sort(key=lambda x: str(x))
    return out


def get_jpeg_blockiness_score(img_path: str) -> float:
    img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0
    img = img.astype(np.float32)
    if img.ndim != 2 or img.shape[0] < 16 or img.shape[1] < 16:
        return 0.0

    diff_h = np.abs(img[:, :-1] - img[:, 1:])
    diff_v = np.abs(img[:-1, :] - img[1:, :])

    h_bounds = float(diff_h[:, 7::8].mean())
    if diff_h.shape[1] <= 8:
        h_non_bounds = float(diff_h.mean())
    else:
        h_non_bounds = float(np.delete(diff_h, np.arange(7, diff_h.shape[1], 8), axis=1).mean())

    v_bounds = float(diff_v[7::8, :].mean())
    if diff_v.shape[0] <= 8:
        v_non_bounds = float(diff_v.mean())
    else:
        v_non_bounds = float(np.delete(diff_v, np.arange(7, diff_v.shape[0], 8), axis=0).mean())

    score = (h_bounds / (h_non_bounds + 1e-5)) + (v_bounds / (v_non_bounds + 1e-5))
    return float(score)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute JPEG blockiness score histogram for a dataset.")
    ap.add_argument("--data-dir", type=Path, required=True, help="Dataset root directory.")
    ap.add_argument("--recursive", action="store_true", help="Scan subdirectories.")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N images (0 means all).")
    ap.add_argument("--bins", type=int, default=200, help="Histogram bins.")
    ap.add_argument("--clip-max", type=float, default=0.0, help="If >0, clip scores to this max before plotting.")
    ap.add_argument("--log-y", action="store_true", help="Use log scale on y axis.")
    ap.add_argument("--out-dir", type=Path, default=Path("."), help="Output directory.")
    args = ap.parse_args()

    root = args.data_dir.resolve()
    paths = iter_image_paths(root, recursive=args.recursive)
    if not paths:
        print(f"未找到图片：{root}", file=sys.stderr)
        sys.exit(1)
    if args.limit and args.limit > 0:
        paths = paths[: args.limit]

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_png = out_dir / "blockiness_hist.png"
    out_npz = out_dir / "blockiness_scores.npz"

    scores: list[float] = []
    for i, p in enumerate(paths):
        if (i % 50) == 0:
            print(f"[{i + 1}/{len(paths)}] {p.name}")
        scores.append(get_jpeg_blockiness_score(str(p)))

    arr = np.asarray(scores, dtype=np.float32)
    if args.clip_max and args.clip_max > 0:
        arr = np.clip(arr, 0.0, float(args.clip_max))

    np.savez_compressed(out_npz, scores=arr)

    q = np.quantile(arr, [0.01, 0.05, 0.5, 0.95, 0.99])
    print(
        f"count={arr.size} mean={float(arr.mean()):.6g} "
        f"p01={float(q[0]):.6g} p05={float(q[1]):.6g} median={float(q[2]):.6g} p95={float(q[3]):.6g} p99={float(q[4]):.6g}"
    )

    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    plt.figure(figsize=(9, 6))
    plt.hist(arr, bins=int(args.bins), color="#777777", edgecolor="#111111")
    if args.log_y:
        plt.yscale("log")
    plt.xlabel("JPEG blockiness score (8x8 boundary / non-boundary)")
    plt.ylabel("Count")
    title = "Blockiness score distribution"
    if args.clip_max and args.clip_max > 0:
        title += f" (clipped to {args.clip_max:g})"
    plt.title(title)
    plt.tight_layout()
    plt.savefig(out_png, dpi=160)
    plt.close()

    print(f"已保存直方图：{out_png}")
    print(f"已保存分数：{out_npz}")


if __name__ == "__main__":
    main()

