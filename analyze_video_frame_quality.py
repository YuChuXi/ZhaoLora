#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:
    print("需要 opencv-python：pip install opencv-python", file=sys.stderr)
    sys.exit(1)

import matplotlib

matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "逐帧分析视频质量趋势，输出每个 mp4 的 CSV 与曲线图。"
            "这里的 quality_score 是视频内相对分数，不是跨视频绝对标准。"
        )
    )
    ap.add_argument(
        "--input-dir",
        type=Path,
        default=Path("datasets/kuriko"),
        help="待扫描目录，默认 datasets/kuriko",
    )
    ap.add_argument(
        "--glob",
        type=str,
        default="*.mp4",
        help="视频匹配模式，默认 *.mp4",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("datasets/kuriko/frame_quality"),
        help="输出目录",
    )
    ap.add_argument(
        "--sample-every",
        type=int,
        default=1,
        help="每隔多少帧采样一次，默认每帧",
    )
    ap.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="每个视频最多处理多少帧，0 表示不限制",
    )
    ap.add_argument(
        "--smooth-window",
        type=int,
        default=11,
        help="曲线平滑窗口大小，默认 11",
    )
    ap.add_argument(
        "--save-top-pct",
        type=float,
        default=0.10,
        help="每秒内按质量分保留前百分比帧并导出 PNG，默认 0.10",
    )
    return ap.parse_args()


def iter_videos(root: Path, pattern: str) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob(pattern) if p.is_file())


def run_ffprobe(video_path: Path) -> list[dict[str, object]]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=pts_time,pkt_size,pict_type,key_frame,width,height",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    frames = payload.get("frames", [])
    if not isinstance(frames, list):
        return []
    return frames


def sanitize_stem(name: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    return s or "video"


def moving_average(arr: np.ndarray, window: int) -> np.ndarray:
    if arr.size == 0:
        return arr
    win = max(1, int(window))
    if win <= 1 or arr.size < 3:
        return arr.copy()
    if win % 2 == 0:
        win += 1
    kernel = np.ones(win, dtype=np.float32) / float(win)
    pad = win // 2
    padded = np.pad(arr, (pad, pad), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def robust_normalize(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values
    lo = float(np.quantile(values, 0.05))
    hi = float(np.quantile(values, 0.95))
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return np.full_like(values, 0.5, dtype=np.float32)
    out = (values - lo) / (hi - lo)
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def frame_sharpness(gray: np.ndarray) -> float:
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    return float(lap.var())


def frame_motion(prev_gray: np.ndarray | None, gray: np.ndarray) -> float:
    if prev_gray is None:
        return 0.0
    diff = cv2.absdiff(prev_gray, gray)
    return float(diff.mean())


def analyze_video(
    video_path: Path,
    ffprobe_frames: list[dict[str, object]],
    sample_every: int,
    max_frames: int,
) -> list[dict[str, object]]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")

    rows: list[dict[str, object]] = []
    prev_gray: np.ndarray | None = None
    decoded_index = 0
    kept_count = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            meta = ffprobe_frames[decoded_index] if decoded_index < len(ffprobe_frames) else {}
            use_this = (decoded_index % sample_every) == 0

            if use_this:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                sharpness = frame_sharpness(gray)
                motion = frame_motion(prev_gray, gray)
                height, width = gray.shape

                pkt_size = int(meta.get("pkt_size", 0) or 0)
                pts_time = float(meta.get("pts_time", 0.0) or 0.0)
                pict_type = str(meta.get("pict_type", "")) or "?"
                key_frame = int(meta.get("key_frame", 0) or 0)
                bpp = float(pkt_size * 8.0 / max(1, width * height))

                rows.append(
                    {
                        "frame_idx": decoded_index,
                        "pts_time": pts_time,
                        "width": width,
                        "height": height,
                        "key_frame": key_frame,
                        "pict_type": pict_type,
                        "pkt_size": pkt_size,
                        "bits_per_pixel": bpp,
                        "sharpness": sharpness,
                        "motion": motion,
                    }
                )
                prev_gray = gray
                kept_count += 1

                if max_frames > 0 and kept_count >= max_frames:
                    break

            decoded_index += 1
    finally:
        cap.release()

    if not rows:
        return rows

    sharp = np.asarray([float(r["sharpness"]) for r in rows], dtype=np.float32)
    bpp = np.asarray([float(r["bits_per_pixel"]) for r in rows], dtype=np.float32)
    motion = np.asarray([float(r["motion"]) for r in rows], dtype=np.float32)

    sharp_n = robust_normalize(np.log1p(sharp))
    bpp_n = robust_normalize(np.log1p(bpp * 1000.0))
    motion_n = robust_normalize(motion)

    quality = 0.75 * sharp_n + 0.25 * bpp_n
    quality_motion_aware = quality * (1.0 - 0.15 * motion_n)

    for i, row in enumerate(rows):
        row["sharpness_norm"] = float(sharp_n[i])
        row["bits_per_pixel_norm"] = float(bpp_n[i])
        row["motion_norm"] = float(motion_n[i])
        row["quality_score"] = float(quality[i])
        row["quality_score_motion_aware"] = float(quality_motion_aware[i])

    return rows


def save_csv(rows: list[dict[str, object]], out_csv: Path) -> None:
    fieldnames = [
        "frame_idx",
        "pts_time",
        "width",
        "height",
        "key_frame",
        "pict_type",
        "pkt_size",
        "bits_per_pixel",
        "sharpness",
        "motion",
        "sharpness_norm",
        "bits_per_pixel_norm",
        "motion_norm",
        "quality_score",
        "quality_score_motion_aware",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def pick_top_rows_per_second(rows: list[dict[str, object]], top_pct: float) -> list[dict[str, object]]:
    top_pct = float(top_pct)
    if top_pct <= 0.0:
        return []
    buckets: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        sec = int(math.floor(float(row["pts_time"])))
        buckets.setdefault(sec, []).append(row)

    picked: list[dict[str, object]] = []
    for sec in sorted(buckets):
        sec_rows = buckets[sec]
        keep_n = max(1, int(math.ceil(len(sec_rows) * top_pct)))
        sec_rows_sorted = sorted(
            sec_rows,
            key=lambda r: (
                float(r["quality_score_motion_aware"]),
                float(r["quality_score"]),
                float(r["sharpness"]),
            ),
            reverse=True,
        )
        picked.extend(sec_rows_sorted[:keep_n])

    picked.sort(key=lambda r: int(r["frame_idx"]))
    return picked


def save_selected_frames_png(
    video_path: Path,
    picked_rows: list[dict[str, object]],
    out_dir: Path,
) -> Path | None:
    if not picked_rows:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "selected_frames.csv"
    frame_map = {int(row["frame_idx"]): row for row in picked_rows}

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")

    saved_rows: list[dict[str, object]] = []
    frame_idx = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            row = frame_map.get(frame_idx)
            if row is not None:
                sec = int(math.floor(float(row["pts_time"])))
                score = float(row["quality_score_motion_aware"])
                out_name = (
                    f"sec_{sec:05d}_frame_{frame_idx:06d}"
                    f"_score_{score:.4f}.png"
                )
                out_path = out_dir / out_name
                if not cv2.imwrite(str(out_path), frame):
                    raise RuntimeError(f"保存 PNG 失败：{out_path}")
                saved_rows.append(
                    {
                        "second": sec,
                        "frame_idx": frame_idx,
                        "pts_time": float(row["pts_time"]),
                        "quality_score_motion_aware": score,
                        "quality_score": float(row["quality_score"]),
                        "sharpness": float(row["sharpness"]),
                        "motion": float(row["motion"]),
                        "png_path": str(out_path),
                    }
                )
                if len(saved_rows) >= len(frame_map):
                    break
            frame_idx += 1
    finally:
        cap.release()

    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "second",
                "frame_idx",
                "pts_time",
                "quality_score_motion_aware",
                "quality_score",
                "sharpness",
                "motion",
                "png_path",
            ],
        )
        writer.writeheader()
        writer.writerows(saved_rows)
    return manifest_path


def plot_rows(rows: list[dict[str, object]], out_png: Path, title: str, smooth_window: int) -> None:
    t = np.asarray([float(r["pts_time"]) for r in rows], dtype=np.float32)
    quality = np.asarray([float(r["quality_score_motion_aware"]) for r in rows], dtype=np.float32)
    sharp = np.asarray([float(r["sharpness"]) for r in rows], dtype=np.float32)
    bpp = np.asarray([float(r["bits_per_pixel"]) for r in rows], dtype=np.float32)
    motion = np.asarray([float(r["motion"]) for r in rows], dtype=np.float32)
    key_mask = np.asarray([int(r["key_frame"]) for r in rows], dtype=np.int32) > 0

    quality_s = moving_average(quality, smooth_window)
    sharp_s = moving_average(np.log1p(sharp), smooth_window)
    bpp_s = moving_average(np.log1p(bpp * 1000.0), smooth_window)
    motion_s = moving_average(motion, smooth_window)

    fig, axes = plt.subplots(4, 1, figsize=(16, 10), sharex=True)

    axes[0].plot(t, quality, color="#7fb069", alpha=0.22, linewidth=1.0)
    axes[0].plot(t, quality_s, color="#2f6b2f", linewidth=1.6)
    axes[0].set_ylabel("quality")
    axes[0].set_ylim(-0.02, 1.02)
    axes[0].grid(alpha=0.25)
    axes[0].set_title(title)

    axes[1].plot(t, np.log1p(sharp), color="#2855a6", alpha=0.25, linewidth=1.0)
    axes[1].plot(t, sharp_s, color="#143b7a", linewidth=1.4)
    axes[1].set_ylabel("log sharp")
    axes[1].grid(alpha=0.25)

    axes[2].plot(t, np.log1p(bpp * 1000.0), color="#c47e1a", alpha=0.25, linewidth=1.0)
    axes[2].plot(t, bpp_s, color="#8f5809", linewidth=1.4)
    axes[2].set_ylabel("log bpp")
    axes[2].grid(alpha=0.25)

    axes[3].plot(t, motion, color="#a54646", alpha=0.22, linewidth=1.0)
    axes[3].plot(t, motion_s, color="#6f2323", linewidth=1.4)
    axes[3].set_ylabel("motion")
    axes[3].set_xlabel("time (sec)")
    axes[3].grid(alpha=0.25)

    key_times = t[key_mask]
    if key_times.size:
        for ax in axes:
            for x in key_times:
                ax.axvline(x, color="#000000", alpha=0.03, linewidth=0.8)

    fig.tight_layout()
    fig.savefig(out_png, dpi=160)
    plt.close(fig)


def summarize_rows(video_path: Path, rows: list[dict[str, object]]) -> dict[str, object]:
    quality = np.asarray([float(r["quality_score_motion_aware"]) for r in rows], dtype=np.float32)
    sharp = np.asarray([float(r["sharpness"]) for r in rows], dtype=np.float32)
    motion = np.asarray([float(r["motion"]) for r in rows], dtype=np.float32)
    bpp = np.asarray([float(r["bits_per_pixel"]) for r in rows], dtype=np.float32)

    return {
        "video": str(video_path),
        "frames": int(len(rows)),
        "duration_sec": float(rows[-1]["pts_time"]) if rows else 0.0,
        "quality_mean": float(quality.mean()),
        "quality_p05": float(np.quantile(quality, 0.05)),
        "quality_p50": float(np.quantile(quality, 0.50)),
        "quality_p95": float(np.quantile(quality, 0.95)),
        "sharpness_mean": float(sharp.mean()),
        "motion_mean": float(motion.mean()),
        "bits_per_pixel_mean": float(bpp.mean()),
    }


def save_summary(summaries: list[dict[str, object]], out_csv: Path) -> None:
    fieldnames = [
        "video",
        "frames",
        "duration_sec",
        "quality_mean",
        "quality_p05",
        "quality_p50",
        "quality_p95",
        "sharpness_mean",
        "motion_mean",
        "bits_per_pixel_mean",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summaries)


def main() -> None:
    args = parse_args()
    if args.sample_every < 1:
        raise SystemExit("--sample-every 必须 >= 1")
    if args.save_top_pct < 0.0 or args.save_top_pct > 1.0:
        raise SystemExit("--save-top-pct 必须在 [0, 1] 之间")

    videos = iter_videos(args.input_dir, args.glob)
    if not videos:
        raise SystemExit(f"未找到视频：{args.input_dir} / {args.glob}")

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[dict[str, object]] = []

    for idx, video_path in enumerate(videos, start=1):
        print(f"[{idx}/{len(videos)}] 分析 {video_path.name}")
        ffprobe_frames = run_ffprobe(video_path)
        rows = analyze_video(
            video_path=video_path,
            ffprobe_frames=ffprobe_frames,
            sample_every=args.sample_every,
            max_frames=args.max_frames,
        )
        if not rows:
            print(f"  跳过：没有读到帧 {video_path.name}", file=sys.stderr)
            continue

        stem = sanitize_stem(video_path.stem)
        video_out_dir = out_dir / stem
        video_out_dir.mkdir(parents=True, exist_ok=True)

        out_csv = video_out_dir / "frame_quality.csv"
        out_png = video_out_dir / "frame_quality_curve.png"
        top_frames_dir = video_out_dir / "top_frames_per_second"

        save_csv(rows, out_csv)
        plot_rows(
            rows,
            out_png,
            title=f"video {idx}  frame quality trend",
            smooth_window=args.smooth_window,
        )
        picked_rows = pick_top_rows_per_second(rows, args.save_top_pct)
        manifest_path = save_selected_frames_png(video_path, picked_rows, top_frames_dir)
        summaries.append(summarize_rows(video_path, rows))
        print(f"  已保存: {out_csv}")
        print(f"  已保存: {out_png}")
        if manifest_path is not None:
            print(f"  已保存: {manifest_path}")

    summary_csv = out_dir / "summary.csv"
    save_summary(summaries, summary_csv)
    print(f"汇总已保存: {summary_csv}")


if __name__ == "__main__":
    main()
