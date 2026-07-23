"""
图像近似去重（对齐 dedupe_dataset_images.py）：
128×128 RGB 指纹 MSE < 阈值 → 同组；组内建议保留策略同脚本。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFile

Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
COMPARE_SIZE = 128


@dataclass
class ImageRecord:
    path: Path
    rel: str
    width: int
    height: int
    blockiness: float
    fingerprint: np.ndarray


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


def _blockiness(gray: np.ndarray) -> float:
    if gray is None or gray.ndim != 2 or gray.shape[0] < 16 or gray.shape[1] < 16:
        return 0.0
    g = gray.astype(np.float32)
    diff_h = np.abs(g[:, :-1] - g[:, 1:])
    diff_v = np.abs(g[:-1, :] - g[1:, :])
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
    return (h_bounds / (h_non_bounds + 1e-5)) + (v_bounds / (v_non_bounds + 1e-5))


def _load_one(path: Path) -> ImageRecord | None:
    gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    blockiness = _blockiness(gray)
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            small = im.resize((COMPARE_SIZE, COMPARE_SIZE), Image.Resampling.LANCZOS)
            arr = np.asarray(small, dtype=np.float32) / 255.0
    except OSError:
        return None
    rel = path.name
    return ImageRecord(path=path, rel=rel, width=w, height=h, blockiness=float(blockiness), fingerprint=arr)


def collect_paths(data_dir: Path) -> list[Path]:
    data_dir = data_dir.resolve()
    out = []
    for p in sorted(data_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            out.append(p)
    return out


def load_records(data_dir: Path, workers: int | None = None) -> list[ImageRecord]:
    paths = collect_paths(data_dir)
    if not paths:
        return []
    workers = workers or max(1, min(16, (os.cpu_count() or 4) + 2))
    records: list[ImageRecord] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_load_one, p): p for p in paths}
        for fut in as_completed(futs):
            rec = fut.result()
            if rec is not None:
                records.append(rec)
    records.sort(key=lambda r: r.rel)
    return records


def _compare_edges(fp_flat: np.ndarray, sq: np.ndarray, thresh: float, j_chunk: int = 2048) -> list[tuple[int, int]]:
    n, d = fp_flat.shape
    edges: list[tuple[int, int]] = []
    inv_d = np.float32(1.0 / float(d))
    thr = np.float32(thresh)
    for i in range(n):
        fi = fp_flat[i]
        di = sq[i]
        for j0 in range(i + 1, n, j_chunk):
            j1 = min(j0 + j_chunk, n)
            dots = fp_flat[j0:j1] @ fi
            mse = (di + sq[j0:j1] - 2.0 * dots) * inv_d
            below = np.nonzero(mse < thr)[0] + j0
            edges.extend((i, int(j)) for j in below)
    return edges


def suggest_keep_index(records: list[ImageRecord], idxs: list[int], similar_res_ratio: float) -> int:
    areas = [records[k].width * records[k].height for k in idxs]
    amax = max(areas)
    amin = min(areas)
    similar = (amin > 0) and ((amax / float(amin)) <= float(similar_res_ratio))
    if similar:
        return min(
            idxs,
            key=lambda k: (
                records[k].blockiness,
                -(records[k].width * records[k].height),
                records[k].rel,
            ),
        )
    return min(
        idxs,
        key=lambda k: (-(records[k].width * records[k].height), records[k].rel),
    )


def find_duplicate_groups(
    records: list[ImageRecord],
    *,
    mse_threshold: float = 1e-4,
    similar_res_ratio: float = 1.05,
    j_chunk: int = 2048,
) -> list[dict]:
    """返回仅含重复的组；每组含 members / keep_rel / suggested_remove。"""
    n = len(records)
    if n < 2:
        return []

    fp = np.stack([r.fingerprint for r in records], axis=0)
    fp_flat = np.ascontiguousarray(fp.reshape(n, -1), dtype=np.float32)
    sq = np.sum(fp_flat * fp_flat, axis=1, dtype=np.float32)
    edges = _compare_edges(fp_flat, sq, float(mse_threshold), j_chunk=j_chunk)

    uf = UnionFind(n)
    for i, j in edges:
        uf.union(i, j)

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(uf.find(i), []).append(i)

    groups: list[dict] = []
    for idxs in buckets.values():
        if len(idxs) < 2:
            continue
        keep = suggest_keep_index(records, idxs, similar_res_ratio)
        members = []
        for k in sorted(idxs, key=lambda x: records[x].rel):
            r = records[k]
            members.append({
                "path": r.rel,
                "stem": Path(r.rel).stem,
                "width": r.width,
                "height": r.height,
                "blockiness": round(r.blockiness, 4),
                "area": r.width * r.height,
                "is_keep": k == keep,
            })
        suggested_remove = [m["path"] for m in members if not m["is_keep"]]
        groups.append({
            "keep": records[keep].rel,
            "size": len(members),
            "members": members,
            "suggested_remove": suggested_remove,
        })

    groups.sort(key=lambda g: (-g["size"], g["keep"]))
    return groups
