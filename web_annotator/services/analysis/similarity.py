"""色调指纹: 优先读缩略图, 磁盘缓存, 矩阵批量比对. 

Callers:
    - ``routes/images.py``: 相似图片查询
    - ``services/dataset/runtime.py:DatasetRuntime``: 数据集颜色索引
"""

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
_CACHE_NAME = "color_fp_v3.npz"
_BUILD_WORKERS = 8


def fingerprint_image(im: Image.Image) -> np.ndarray:
    """由 PIL 图计算色调指纹向量(HSV 直方图 + RGB/HSV 均值, 归一化为单位向量). 

    Args:
        im: PIL 图片对象. 

    Returns:
        np.ndarray: 归一化指纹向量, 形状 ``(H_BINS + S_BINS + V_BINS + 6,)``. 

    Callers:
        - ``fingerprint_file``
    """
    small = im.resize((_FP_SIZE, _FP_SIZE), Image.Resampling.BILINEAR)
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

    # 色相仅对彩色像素有意义: 按饱和度加权, 灰/白/黑像素(s≈0)不参与色相分布,
    # 避免它们以 h=0 制造红色尖峰, 导致白色背景图被误判为彼此相似.
    sat = float(s.sum())
    h_hist, _ = np.histogram(h, bins=_H_BINS, range=(0.0, 1.0), weights=s)
    if sat > 0:
        h_hist = _H_BINS * h_hist / sat
    s_hist, _ = np.histogram(s, bins=_S_BINS, range=(0.0, 1.0), density=True)
    v_hist, _ = np.histogram(v, bins=_V_BINS, range=(0.0, 1.0), density=True)
    h_mean = float((h * s).sum() / sat) if sat > 0 else 0.0
    mean = np.array(
        [float(r.mean()), float(g.mean()), float(b.mean()),
         h_mean, float(s.mean()), float(v.mean())],
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
    """读取图片文件计算色调指纹；解码失败时返回 None. 

    Args:
        path: 图片文件路径. 

    Returns:
        np.ndarray | None: 归一化指纹向量, 或解码失败时为 None. 

    Callers:
        - ``ColorIndex.fingerprint_for``
        - ``ColorIndex.ensure_many``
        - ``routes/images.py``
    """
    if not path.is_file():
        return None
    with Image.open(path) as im:
        return fingerprint_image(im.convert("RGB"))


def distance(a: np.ndarray, b: np.ndarray) -> float:
    """计算两个指纹向量的 L2 欧氏距离. 

    Args:
        a: 指纹向量 A. 
        b: 指纹向量 B. 

    Returns:
        float: 欧氏距离. 

    Callers:
        - ``ColorIndex.similar_matrix``
    """
    return float(np.linalg.norm(a - b))


class ColorIndex:
    """内存 + 磁盘(npz)色调指纹索引, 线程安全. 

    优先经由缩略图加速指纹计算, 通过 ``ensure_many`` 批量构建, 
    ``similar_matrix`` 矩阵比对返回最近邻. 

    Callers:
        - ``services/dataset/runtime.py:DatasetRuntime``
        - ``routes/images.py``
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        """初始化索引. 

        Args:
            cache_dir: npz 缓存目录；为 None 则仅内存模式. 

        Callers:
            - ``DatasetRuntime.__init__``
        """
        self._lock = threading.RLock()
        self._fps: dict[str, tuple[float, np.ndarray]] = {}
        self._cache_path = (cache_dir / _CACHE_NAME) if cache_dir else None
        self._dirty = False
        if self._cache_path:
            self._load_disk()

    def set_cache_dir(self, cache_dir: Path) -> None:
        """设置或更改磁盘指纹缓存目录. 

        Args:
            cache_dir: npz 缓存所在目录. 

        Callers:
            - ``routes/images.py``
        """
        with self._lock:
            self._cache_path = cache_dir / _CACHE_NAME
            cache_dir.mkdir(parents=True, exist_ok=True)
            if not self._fps:
                self._load_disk()

    def clear(self) -> None:
        """清空内存中的全部指纹(标注删除或去重移动后调用). 

        Callers:
            - ``routes/annotations.py``
            - ``routes/dedupe.py``
        """
        with self._lock:
            self._fps.clear()
            self._dirty = True

    def put(self, rel: str, mtime: float, fp: np.ndarray) -> None:
        """写入或更新单条指纹. 

        Args:
            rel: 图片相对路径. 
            mtime: 文件修改时间(用于缓存失效判定). 
            fp: 指纹向量. 

        Callers:
            - ``ColorIndex.fingerprint_for``
            - ``ColorIndex.ensure_many``
            - ``routes/images.py``
        """
        with self._lock:
            self._fps[rel] = (mtime, fp.astype(np.float32, copy=False))
            self._dirty = True

    def _load_disk(self) -> None:
        """从 npz 文件加载缓存到内存. 

        Callers:
            - ``__init__``
            - ``set_cache_dir``
        """
        if not self._cache_path or not self._cache_path.is_file():
            return
        data = np.load(self._cache_path, allow_pickle=True)
        rels = data["rels"]
        mtimes = data["mtimes"]
        matrix = data["matrix"]
        for i, rel in enumerate(rels.tolist()):
            self._fps[str(rel)] = (float(mtimes[i]), matrix[i].astype(np.float32, copy=False))

    def flush(self) -> None:
        """将内存中的指纹原子写入 npz 文件(仅在 dirty 时生效). 

        Callers:
            - ``ensure_many``
        """
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
        """取单个相对路径的色调指纹(若缩略图可用则经由缩略图加速). 

        mtime 变化时自动重新计算. 

        Args:
            data_dir: 数据集根目录. 
            rel: 相对路径. 
            thumb_path: 缩略图路径, 非 None 且存在时优先使用. 

        Returns:
            np.ndarray | None: 指纹向量, 或文件缺失/解码失败时为 None. 

        Callers:
            - ``ensure_many``
        """
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
        """批量确保候选图片的指纹已加载到内存(并行计算缺失项, 完成后写入磁盘). 

        Args:
            data_dir: 数据集根目录. 
            rels: 图片相对路径列表. 
            thumb_paths: 可选的 ``{相对路径: 缩略图路径}`` 映射. 

        Callers:
            - ``routes/images.py``
        """
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
            """为单张图计算指纹. 引用: ``ensure_many``. """
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
        """矩阵批量比对, 返回与 query 最近的 limit 个候选. 

        Args:
            query: 查询指纹向量. 
            candidates: 候选图片相对路径列表. 
            exclude: 排除的相对路径(如自身). 
            limit: 返回的最多结果数. 

        Returns:
            ``[(相对路径, L2距离), ...]``, 按距离升序排列. 

        Callers:
            - ``routes/images.py``
        """
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
