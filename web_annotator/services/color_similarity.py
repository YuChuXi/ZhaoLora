"""色调指纹：优先读缩略图，磁盘缓存，矩阵批量比对。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

_FP_SIZE = 32
_H_BINS = 12
_S_BINS = 6
_V_BINS = 6
_CACHE_NAME = "color_fp_v2.npz"
_BUILD_WORKERS = 8


def fingerprint_image(im: Image.Image) -> np.ndarray:
    """由 PIL 图计算色调指纹向量。引用：fingerprint_file。"""
    small = im.resize((_FP_SIZE, _FP_SIZE), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32) * (1.0 / 255.0)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    diff = mx - mn
    v = mx
    s = np.zeros_like(mx)
    nonzero = mx > 1e-6
    s[nonzero] = diff[nonzero] / mx[nonzero]
    h = np.zeros_like(mx)
    mask = diff > 1e-6
    if np.any(mask):
        rc = (mx[mask] - r[mask]) / (diff[mask] + 1e-8)
        gc = (mx[mask] - g[mask]) / (diff[mask] + 1e-8)
        bc = (mx[mask] - b[mask]) / (diff[mask] + 1e-8)
        hr = ((bc - gc) / 6.0) % 1.0
        hg = (2.0 + rc - bc) / 6.0
        hb = (4.0 + gc - rc) / 6.0
        h[mask] = np.where(mx[mask] == r[mask], hr, np.where(mx[mask] == g[mask], hg, hb))

    h_hist, _ = np.histogram(h, bins=_H_BINS, range=(0.0, 1.0), density=True)
    s_hist, _ = np.histogram(s, bins=_S_BINS, range=(0.0, 1.0), density=True)
    v_hist, _ = np.histogram(v, bins=_V_BINS, range=(0.0, 1.0), density=True)
    mean = np.array(
        [float(r.mean()), float(g.mean()), float(b.mean()),
         float(h.mean()), float(s.mean()), float(v.mean())],
        dtype=np.float32,
    )
    vec = np.concatenate([
        h_hist.astype(np.float32),
        s_hist.astype(np.float32),
        v_hist.astype(np.float32),
        mean,
    ])
    n = float(np.linalg.norm(vec))
    if n > 1e-8:
        vec /= n
    return vec


def fingerprint_file(path: Path) -> np.ndarray | None:
    """读文件计算指纹；失败返回 None。引用：ColorIndex / routes.images。"""
    if not path.is_file():
        return None
    with Image.open(path) as im:
        return fingerprint_image(im.convert("RGB"))


def distance(a: np.ndarray, b: np.ndarray) -> float:
    """两指纹 L2 距离。引用：similar_matrix。"""
    return float(np.linalg.norm(a - b))


class ColorIndex:
    """内存 + 磁盘色调索引。"""

    def __init__(self, cache_dir: Path | None = None) -> None:
        """内存色调索引。引用：DatasetRuntime.color_index。"""
        self._lock = threading.RLock()
        self._fps: dict[str, tuple[float, np.ndarray]] = {}
        self._cache_path = (cache_dir / _CACHE_NAME) if cache_dir else None
        self._dirty = False
        if self._cache_path:
            self._load_disk()

    def set_cache_dir(self, cache_dir: Path) -> None:
        """设置磁盘指纹缓存目录。引用：routes.images。"""
        with self._lock:
            self._cache_path = cache_dir / _CACHE_NAME
            cache_dir.mkdir(parents=True, exist_ok=True)
            if not self._fps:
                self._load_disk()

    def clear(self) -> None:
        """清空内存指纹。引用：annotations / dedupe 删除后。"""
        with self._lock:
            self._fps.clear()
            self._dirty = True

    def put(self, rel: str, mtime: float, fp: np.ndarray) -> None:
        """写入单条指纹。引用：routes.images.similar_images。"""
        with self._lock:
            self._fps[rel] = (mtime, fp.astype(np.float32, copy=False))
            self._dirty = True

    def _load_disk(self) -> None:
        """从 npz 加载缓存。引用：ensure_many。"""
        if not self._cache_path or not self._cache_path.is_file():
            return
        data = np.load(self._cache_path, allow_pickle=True)
        rels = data["rels"]
        mtimes = data["mtimes"]
        matrix = data["matrix"]
        for i, rel in enumerate(rels.tolist()):
            self._fps[str(rel)] = (float(mtimes[i]), matrix[i].astype(np.float32, copy=False))

    def flush(self) -> None:
        """把内存指纹写回 npz。引用：ensure_many。"""
        with self._lock:
            if not self._dirty or not self._cache_path:
                return
            if not self._fps:
                return
            rels = list(self._fps.keys())
            mtimes = np.array([self._fps[r][0] for r in rels], dtype=np.float64)
            matrix = np.stack([self._fps[r][1] for r in rels], axis=0)
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp.npz")
            np.savez_compressed(tmp, rels=np.array(rels, dtype=object), mtimes=mtimes, matrix=matrix)
            tmp.replace(self._cache_path)
            self._dirty = False

    def fingerprint_for(
        self,
        data_dir: Path,
        rel: str,
        thumb_path: Path | None = None,
    ) -> np.ndarray | None:
        """取相对路径指纹（可走缩略图）。引用：ensure_many。"""
        path = data_dir / rel
        if not path.is_file():
            with self._lock:
                self._fps.pop(rel, None)
            return None
        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._fps.get(rel)
            if cached and cached[0] == mtime:
                return cached[1]

        src = thumb_path if (thumb_path is not None and thumb_path.is_file()) else path
        fp = fingerprint_file(src)
        if fp is None:
            with self._lock:
                self._fps.pop(rel, None)
            return None
        self.put(rel, mtime, fp)
        return fp

    def ensure_many(
        self,
        data_dir: Path,
        rels: list[str],
        thumb_paths: dict[str, Path] | None = None,
    ) -> None:
        """批量确保候选指纹在内存中。引用：routes.images。"""
        thumb_paths = thumb_paths or {}
        missing: list[str] = []
        with self._lock:
            for rel in rels:
                path = data_dir / rel
                if not path.is_file():
                    continue
                mtime = path.stat().st_mtime
                cached = self._fps.get(rel)
                if not cached or cached[0] != mtime:
                    missing.append(rel)
        if not missing:
            return

        def one(rel: str) -> tuple[str, float, np.ndarray] | None:
            """为单张图计算指纹。引用：``ensure_many``。"""
            path = data_dir / rel
            if not path.is_file():
                return None
            mtime = path.stat().st_mtime
            thumb = thumb_paths.get(rel)
            src = thumb if (thumb is not None and thumb.is_file()) else path
            fp = fingerprint_file(src)
            if fp is None:
                return None
            return rel, mtime, fp

        with ThreadPoolExecutor(max_workers=_BUILD_WORKERS) as pool:
            futs = [pool.submit(one, rel) for rel in missing]
            for fut in as_completed(futs):
                item = fut.result()
                if item is None:
                    continue
                rel, mtime, fp = item
                self.put(rel, mtime, fp)
        self.flush()

    def similar_matrix(
        self,
        query: np.ndarray,
        candidates: list[str],
        exclude: str | None = None,
        limit: int = 8,
    ) -> list[tuple[str, float]]:
        """矩阵比对返回最近邻。引用：routes.images。"""
        with self._lock:
            rows: list[str] = []
            vecs: list[np.ndarray] = []
            for rel in candidates:
                if exclude and rel == exclude:
                    continue
                cached = self._fps.get(rel)
                if not cached:
                    continue
                rows.append(rel)
                vecs.append(cached[1])
        if not rows:
            return []
        mat = np.stack(vecs, axis=0)
        dists = np.linalg.norm(mat - query.reshape(1, -1), axis=1)
        if limit >= len(rows):
            order = np.argsort(dists)
        else:
            order = np.argpartition(dists, limit)[:limit]
            order = order[np.argsort(dists[order])]
        return [(rows[i], float(dists[i])) for i in order[:limit]]
