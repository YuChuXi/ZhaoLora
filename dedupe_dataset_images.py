#!/usr/bin/env python3
"""
对指定目录（递归）内的图像按 128×128 缩放后的逐像素 MSE 做近似去重：
若 mean((a-b)^2) < 阈值，则视为同一张图；保留原始分辨率（宽×高）更大的文件，删除其余。

默认同时删除与图片同 stem 的 .txt 旁路文件（与 prepare_metadata 数据集布局一致）。

比较阶段默认单进程（--compare-workers=1），由所链接的 BLAS（OpenBLAS/MKL 等）按 OMP_NUM_THREADS 等环境变量多线程执行矩阵运算。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import warnings

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np

from PIL import Image
import cv2

# 本地数据集常见千万～上亿像素图，取消 Pillow 解压炸弹默认上限（仅缩到 128 用于指纹）
Image.MAX_IMAGE_PIXELS = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
COMPARE_SIZE = 128


@dataclass(frozen=True)
class ImageRecord:
    path: Path
    width: int
    height: int
    blockiness: float  # 8x8 网格效应（越大越像 JPEG 压缩残留）
    fingerprint: np.ndarray  # shape (COMPARE_SIZE, COMPARE_SIZE, 3), float32 [0,1]


def _collect_image_paths(root: Path, recursive: bool) -> list[Path]:
    root = root.resolve()
    if not root.is_dir():
        return []
    out: list[Path] = []
    if recursive:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in IMAGE_EXTS:
                    out.append(p)
    else:
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.append(p)
    out.sort(key=lambda x: str(x))
    return out


def _load_one(path_str: str) -> ImageRecord | None:
    path = Path(path_str)
    try:
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None or gray.ndim != 2 or gray.shape[0] < 16 or gray.shape[1] < 16:
            blockiness = 0.0
        else:
            g = gray.astype(np.float32)
            diff_h = np.abs(g[:, :-1] - g[:, 1:])
            diff_v = np.abs(g[:-1, :] - g[1:, :])

            h_bounds = float(diff_h[:, 7::8].mean())
            if diff_h.shape[1] <= 8:
                h_non_bounds = float(diff_h.mean())
            else:
                h_non_bounds = float(
                    np.delete(diff_h, np.arange(7, diff_h.shape[1], 8), axis=1).mean()
                )

            v_bounds = float(diff_v[7::8, :].mean())
            if diff_v.shape[0] <= 8:
                v_non_bounds = float(diff_v.mean())
            else:
                v_non_bounds = float(
                    np.delete(diff_v, np.arange(7, diff_v.shape[0], 8), axis=0).mean()
                )

            blockiness = (h_bounds / (h_non_bounds + 1e-5)) + (v_bounds / (v_non_bounds + 1e-5))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UserWarning)
            warnings.simplefilter("ignore", category=Image.DecompressionBombWarning)
            with Image.open(path) as im:
                im = im.convert("RGB")
                w, h = im.size
                small = im.resize((COMPARE_SIZE, COMPARE_SIZE), Image.Resampling.LANCZOS)
                arr = np.asarray(small, dtype=np.float32) / 255.0
    except OSError:
        return None
    return ImageRecord(path=path, width=w, height=h, blockiness=float(blockiness), fingerprint=arr)


class UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _compare_row_range(
    i_start: int,
    i_end: int,
    fp_flat: np.ndarray,
    sq: np.ndarray,
    thresh: float,
    j_chunk: int,
) -> list[tuple[int, int]]:
    """
    fp_flat: (n, D) float32 C-连续；sq[i]=sum_k fp_flat[i,k]^2。
    MSE(i,j)=mean((fi-fj)^2)=(sq[i]+sq[j]-2*dot(fi,fj))/D，避免构造 (chunk,H,W,C) 差分张量。
    """
    n, D = fp_flat.shape
    edges: list[tuple[int, int]] = []
    inv_d = np.float32(1.0 / float(D))
    thr = np.float32(thresh)
    for i in range(i_start, min(i_end, n)):
        fi = fp_flat[i]
        di = sq[i]
        for j0 in range(i + 1, n, j_chunk):
            j1 = min(j0 + j_chunk, n)
            dots = fp_flat[j0:j1] @ fi
            mse = (di + sq[j0:j1] - 2.0 * dots) * inv_d
            below = np.nonzero(mse < thr)[0] + j0
            edges.extend((i, int(j)) for j in below)
    return edges


def _compare_row_range_shm(
    packed: tuple[str, int, int, float, int, int, int],
) -> list[tuple[int, int]]:
    """SharedMemory 布局：前 n*D*4 字节为 fp_flat，随后 n*4 字节为 sq（float32）。"""
    shm_name, n, d, thresh, j_chunk, a, b = packed
    fp_nbytes = n * d * 4
    sq_nbytes = n * 4
    sh = shared_memory.SharedMemory(name=shm_name)
    try:
        buf = sh.buf
        fp_flat = np.ndarray((n, d), dtype=np.float32, buffer=buf[:fp_nbytes])
        sq = np.ndarray((n,), dtype=np.float32, buffer=buf[fp_nbytes : fp_nbytes + sq_nbytes])
        return _compare_row_range(a, b, fp_flat, sq, thresh, j_chunk)
    finally:
        sh.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="数据集根目录",
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="不递归子目录，仅扫描根目录一层",
    )
    p.add_argument(
        "--mse-threshold",
        type=float,
        default=1e-4,
        help="128×128 RGB 归一化到 [0,1] 后的 mean squared error 上限（默认 1e-4）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将移除/移动的文件，不实际执行",
    )
    p.add_argument(
        "--load-workers",
        type=int,
        default=max(1, min(32, (os.cpu_count() or 4) + 4)),
        help="并行加载/解码图像的线程数",
    )
    p.add_argument(
        "--compare-workers",
        type=int,
        default=1,
        help=(
            "两两比较进程数。默认 1：单进程内用 OpenBLAS/MKL 等多线程加速 @ 与向量运算（可用环境变量 "
            "OMP_NUM_THREADS / OPENBLAS_NUM_THREADS / MKL_NUM_THREADS 控制线程数）。"
            "设 >1 为多进程分块比较，若仍很慢可对该进程 export OMP_NUM_THREADS=1 避免与多进程叠乘。"
        ),
    )
    p.add_argument(
        "--j-chunk",
        type=int,
        default=2048,
        help="沿 j 轴分块大小（点积版仅需 O(chunk) 临时向量，可设较大）",
    )
    p.add_argument(
        "--no-sidecar-txt",
        action="store_true",
        help="处理重复图时不处理同名的 .txt",
    )
    p.add_argument(
        "--similar-res-ratio",
        type=float,
        default=1.05,
        help="同一组内若最大面积/最小面积 <= 该值，则认为分辨率差异不大（默认 1.05）",
    )
    p.add_argument(
        "--trash-dir",
        type=Path,
        default=None,
        help="将重复文件移动到该目录（保持相对 data-dir 的路径）；不指定则直接删除",
    )
    return p.parse_args()


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem = p.stem
    suffix = p.suffix
    parent = p.parent
    for i in range(1, 10_000):
        cand = parent / f"{stem}__dup{i}{suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"无法生成不冲突的目标文件名: {p}")


def _move_keep_rel(src: Path, data_root: Path, trash_root: Path) -> Path:
    rel = src.resolve().relative_to(data_root.resolve())
    dst = (trash_root / rel).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst = _unique_path(dst)
    shutil.move(str(src), str(dst))
    return dst


def main() -> None:
    args = _parse_args()
    root = args.data_dir.resolve()
    if not root.is_dir():
        print(f"错误: 目录不存在 {root}", file=sys.stderr)
        sys.exit(1)

    paths = _collect_image_paths(root, recursive=not args.no_recursive)
    if len(paths) < 2:
        print(f"图像数量不足 2，跳过（共 {len(paths)} 张）")
        return

    print(f"扫描到 {len(paths)} 张图像，加载指纹（{COMPARE_SIZE}×{COMPARE_SIZE}）…")
    records: list[ImageRecord] = []
    failed: list[Path] = []

    with ThreadPoolExecutor(max_workers=args.load_workers) as ex:
        futs = {ex.submit(_load_one, str(p)): p for p in paths}
        for fut in as_completed(futs):
            p = futs[fut]
            rec = fut.result()
            if rec is None:
                failed.append(p)
            else:
                records.append(rec)

    if failed:
        print(f"警告: {len(failed)} 个文件无法读取，已跳过", file=sys.stderr)

    records.sort(key=lambda r: str(r.path))
    n = len(records)
    if n < 2:
        print("有效图像不足 2，退出")
        return

    fp = np.stack([r.fingerprint for r in records], axis=0)
    fp_flat = np.ascontiguousarray(fp.reshape(n, -1), dtype=np.float32)
    d = fp_flat.shape[1]
    sq = np.sum(fp_flat * fp_flat, axis=1, dtype=np.float32)
    thresh = float(args.mse_threshold)

    print(f"两两比较 MSE < {thresh:g}（compare-workers={args.compare_workers}，D={d}）…")
    edges: list[tuple[int, int]] = []
    if args.compare_workers <= 1:
        edges.extend(_compare_row_range(0, n, fp_flat, sq, thresh, args.j_chunk))
    else:
        chunk = max(1, (n + args.compare_workers - 1) // args.compare_workers)
        ranges = [(i, min(i + chunk, n)) for i in range(0, n, chunk)]
        fp_nbytes = fp_flat.nbytes
        sq_nbytes = sq.nbytes
        shm = shared_memory.SharedMemory(create=True, size=fp_nbytes + sq_nbytes)
        try:
            buf = shm.buf
            fp_view = np.ndarray((n, d), dtype=np.float32, buffer=buf[:fp_nbytes])
            sq_view = np.ndarray((n,), dtype=np.float32, buffer=buf[fp_nbytes : fp_nbytes + sq_nbytes])
            fp_view[:] = fp_flat
            sq_view[:] = sq
            shm_name = shm.name
            packed_ranges = [
                (shm_name, n, d, thresh, args.j_chunk, a, b) for a, b in ranges
            ]
            with ProcessPoolExecutor(max_workers=args.compare_workers) as ex:
                parts = ex.map(_compare_row_range_shm, packed_ranges)
            for part in parts:
                edges.extend(part)
        finally:
            shm.close()
            shm.unlink()

    uf = UnionFind(n)
    for i, j in edges:
        uf.union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        r = uf.find(i)
        groups.setdefault(r, []).append(i)

    to_delete: list[Path] = []
    sidecar_txt: list[Path] = []

    for _root_idx, idxs in groups.items():
        if len(idxs) == 1:
            continue
        areas = [records[k].width * records[k].height for k in idxs]
        amax = max(areas)
        amin = min(areas)
        similar = (amin > 0) and ((amax / float(amin)) <= float(args.similar_res_ratio))

        if similar:
            # 分辨率差异不大：优先保留 blockiness 更低（更少 JPEG 网格残留）的那张
            best = min(
                idxs,
                key=lambda k: (
                    records[k].blockiness,
                    -(records[k].width * records[k].height),
                    str(records[k].path),
                ),
            )
        else:
            # 分辨率差异大：优先保留更大分辨率
            best = min(
                idxs,
                key=lambda k: (-(records[k].width * records[k].height), str(records[k].path)),
            )
        for k in idxs:
            if k == best:
                continue
            p = records[k].path
            to_delete.append(p)
            if not args.no_sidecar_txt:
                cap = p.with_suffix(".txt")
                if cap.is_file():
                    sidecar_txt.append(cap)

    to_delete.sort(key=lambda x: str(x))
    sidecar_txt = sorted({p.resolve() for p in sidecar_txt}, key=lambda x: str(x))

    verb = "移动" if args.trash_dir is not None else "删除"
    print(
        f"发现 {len(groups)} 个连通组，其中 {sum(1 for v in groups.values() if len(v) > 1)} 组含重复；将{verb} {len(to_delete)} 张图",
        end="",
    )
    if not args.no_sidecar_txt:
        print(f"，及 {len(sidecar_txt)} 个旁路 .txt", end="")
    print()

    if args.trash_dir is not None:
        trash_root = args.trash_dir.resolve()
        print(f"目标目录: {trash_root}")
    else:
        trash_root = None

    for p in to_delete:
        if trash_root is None:
            print(f"  DEL {p}")
        else:
            rel = p.resolve().relative_to(root)
            print(f"  MOV {p} -> {trash_root / rel}")
    for p in sidecar_txt:
        if trash_root is None:
            print(f"  DEL {p}")
        else:
            rel = p.resolve().relative_to(root)
            print(f"  MOV {p} -> {trash_root / rel}")

    if args.dry_run:
        print(f"[dry-run] 未{verb}任何文件")
        return

    if trash_root is None:
        for p in to_delete:
            try:
                p.unlink()
            except OSError as e:
                print(f"删除失败 {p}: {e}", file=sys.stderr)
        for p in sidecar_txt:
            try:
                p.unlink()
            except OSError as e:
                print(f"删除失败 {p}: {e}", file=sys.stderr)
    else:
        for p in to_delete:
            try:
                _move_keep_rel(p, root, trash_root)
            except (OSError, RuntimeError, ValueError) as e:
                print(f"移动失败 {p}: {e}", file=sys.stderr)
        for p in sidecar_txt:
            try:
                _move_keep_rel(p, root, trash_root)
            except (OSError, RuntimeError, ValueError) as e:
                print(f"移动失败 {p}: {e}", file=sys.stderr)

    print("完成")


if __name__ == "__main__":
    # 多进程比较需要可 pickle 的入口；Windows 下需此守卫
    main()
