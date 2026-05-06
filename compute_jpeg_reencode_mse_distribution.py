#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from io import BytesIO
from pathlib import Path

import numpy as np

try:
    from PIL import Image
except ImportError:
    print("需要 Pillow：pip install pillow", file=sys.stderr)
    sys.exit(1)

# Cancel Pillow image pixel safety limit.
Image.MAX_IMAGE_PIXELS = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def parse_qualities(s: str) -> list[int]:
    parts = [x.strip() for x in s.split(",") if x.strip()]
    if not parts:
        raise ValueError("qualities 不能为空")
    qs: list[int] = []
    for p in parts:
        q = int(p)
        if q < 1 or q > 100:
            raise ValueError(f"quality 必须在 [1, 100]，但得到 {q}")
        qs.append(q)
    return qs


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


def downscale_max_edge(im: Image.Image, max_edge: int) -> Image.Image:
    w, h = im.size
    m = max(w, h)
    if m <= max_edge:
        return im
    scale = max_edge / float(m)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return im.resize((nw, nh), Image.Resampling.LANCZOS)


def rgb_to_luma(rgb01: np.ndarray) -> np.ndarray:
    # rgb01: (H,W,3) float32 in [0,1]
    r = rgb01[..., 0]
    g = rgb01[..., 1]
    b = rgb01[..., 2]
    return (0.299 * r + 0.587 * g + 0.114 * b).astype(np.float32)


def mse(a: np.ndarray, b: np.ndarray) -> float:
    # Both arrays must be same shape.
    d = a - b
    return float(np.mean(d * d))


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute JPEG-reencode MSE distribution for a dataset.")
    ap.add_argument("--data-dir", type=Path, required=True, help="Dataset image root directory.")
    ap.add_argument("--recursive", action="store_true", help="Scan subdirectories.")
    ap.add_argument("--max-edge", type=int, default=256, help="Downscale max side before computing MSE.")
    ap.add_argument("--qualities", type=str, default="20,30,40,50,60,70,80,90", help="Comma-separated JPEG qualities.")
    ap.add_argument("--bins", type=int, default=200, help="Histogram bins.")
    ap.add_argument("--log-y", action="store_true", help="Plot histogram with log-scaled y axis.")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N images (0 means all).")
    ap.add_argument("--out-dir", type=Path, default=Path("."), help="Output directory for CSV and PNG.")
    args = ap.parse_args()

    qualities = parse_qualities(args.qualities)
    root = args.data_dir.resolve()
    paths = iter_image_paths(root, recursive=args.recursive)
    if not paths:
        print(f"No images found in: {root}", file=sys.stderr)
        sys.exit(1)

    if args.limit and args.limit > 0:
        paths = paths[: args.limit]

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    out_csv = out_dir / "mse_by_quality.csv"
    out_npz = out_dir / "mse_values.npz"
    out_hist = out_dir / "mse_hist.png"

    print(f"Found {len(paths)} images. qualities={qualities}. max-edge={args.max_edge}")

    # stats containers
    mse_all: list[float] = []
    mse_by_q: dict[int, list[float]] = defaultdict(list)
    rows: list[tuple[str, int, float]] = []

    failed_paths: list[str] = []

    # Process sequentially to control memory usage.
    for i, p in enumerate(paths):
        if (i % 10) == 0:
            print(f"[{i + 1}/{len(paths)}] {p.name}")

        try:
            with Image.open(p) as im0:
                im0 = im0.convert("RGB")
                im0 = downscale_max_edge(im0, args.max_edge)
                rgb01 = np.asarray(im0, dtype=np.float32) / 255.0
                y = rgb_to_luma(rgb01)

                for q in qualities:
                    buf = BytesIO()
                    # Re-encode as JPEG with the same pixel grid size.
                    im0.save(buf, format="JPEG", quality=int(q))
                    buf.seek(0)
                    dec = Image.open(buf).convert("RGB")
                    dec01 = np.asarray(dec, dtype=np.float32) / 255.0
                    y_dec = rgb_to_luma(dec01)
                    val = mse(y, y_dec)

                    mse_all.append(val)
                    mse_by_q[q].append(val)
                    rows.append((str(p), int(q), val))
        except (OSError, ValueError):
            failed_paths.append(str(p))
            continue

    if failed_paths:
        print(f"Warning: failed to process {len(failed_paths)} images.", file=sys.stderr)

    # Save CSV
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "quality", "mse"])
        w.writerows(rows)

    # Save NPZ for later analysis.
    np.savez_compressed(
        out_npz,
        mse_all=np.asarray(mse_all, dtype=np.float32),
        mse_by_q={str(q): np.asarray(vals, dtype=np.float32) for q, vals in mse_by_q.items()},
    )

    arr = np.asarray(mse_all, dtype=np.float32)
    if arr.size == 0:
        print("No MSE values computed.", file=sys.stderr)
        sys.exit(1)

    def stat(a: np.ndarray) -> dict[str, float]:
        return {
            "count": float(a.size),
            "mean": float(np.mean(a)),
            "median": float(np.median(a)),
            "p01": float(np.quantile(a, 0.01)),
            "p05": float(np.quantile(a, 0.05)),
            "p95": float(np.quantile(a, 0.95)),
            "p99": float(np.quantile(a, 0.99)),
        }

    s_all = stat(arr)
    print("Overall MSE stats:")
    print(
        f"  count={int(s_all['count'])} mean={s_all['mean']:.6g} median={s_all['median']:.6g} "
        f"p01={s_all['p01']:.6g} p05={s_all['p05']:.6g} p95={s_all['p95']:.6g} p99={s_all['p99']:.6g}"
    )

    print("Per-quality MSE stats:")
    for q in qualities:
        a = np.asarray(mse_by_q[q], dtype=np.float32)
        if a.size == 0:
            continue
        s = stat(a)
        print(
            f"  q={q}: count={int(s['count'])} mean={s['mean']:.6g} median={s['median']:.6g} "
            f"p05={s['p05']:.6g} p95={s['p95']:.6g}"
        )

    # Plot histogram (optional if matplotlib missing).
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        plt.figure(figsize=(9, 6))
        plt.hist(arr, bins=int(args.bins), color="#777777", edgecolor="#111111")
        if args.log_y:
            plt.yscale("log")
        plt.xlabel("MSE (luma, float in [0,1])")
        plt.ylabel("Count")
        plt.title("JPEG Re-encode MSE Distribution (all images)")
        plt.tight_layout()
        plt.savefig(out_hist, dpi=160)
        plt.close()
        print(f"Saved histogram: {out_hist}")
    except ImportError:
        print("matplotlib 未安装，跳过直方图，仅保存 CSV/NPZ。", file=sys.stderr)

    print(f"Saved CSV: {out_csv}")
    print(f"Saved NPZ: {out_npz}")


if __name__ == "__main__":
    main()

