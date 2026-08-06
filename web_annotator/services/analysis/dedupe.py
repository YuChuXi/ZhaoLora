"""图像近似去重: 指纹 MSE 分组, 组内建议保留更清晰样本. """

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import cv2
import numpy as np
from PIL import Image, ImageFile, UnidentifiedImageError

from services.core.scanner import IMAGE_EXTS
from services.core.device import cuda_device

if TYPE_CHECKING:  # torch 惰性导入, 仅用于类型注解
    import torch

Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True

COMPARE_SIZE = 128
FFT_SIZE = 64
FFT_LOW = 16
ORB_MAX = 512
ORB_NFEATURES = 1000
ORB_RATIO = 0.75


@dataclass
class ImageRecord:
    path: Path
    rel: str
    width: int
    height: int
    blockiness: float
    fingerprint: np.ndarray
    fft_feature: np.ndarray | None = None
    orb_des: np.ndarray | None = None


class UnionFind:
    def __init__(self, n: int) -> None:
        """并查集初始化. 引用: find_duplicate_groups. """
        self.p = list(range(n))

    def find(self, x: int) -> int:
        """查找并压缩路径. 引用: union / find_duplicate_groups. """
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        """合并两个集合. 引用: find_duplicate_groups. """
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _blockiness(gray: np.ndarray) -> float:
    """估计 JPEG 块效应强度. 引用: _load_one. """
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
    """读图并生成 128×128 指纹. 引用: load_records. """
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


def _fft_feature_one(img: Image.Image, device=None) -> np.ndarray:
    """提取单张图片的 FFT 低频幅度特征.

    将图像灰度化并缩放到 ``FFT_SIZE`` 平方, 去均值后做 2D FFT 并平移到
    低频居中, 取中心 ``FFT_LOW`` 方块幅度谱展平为特征向量并做 L2 归一化.
    幅度谱对平移不变, 适合判断内容相似但位置偏移的图像. 灰度化、降采样与
    FFT 均在 ``device`` 指定的 CUDA 设备上计算, ``device`` 为 ``None`` 时
    回退 numpy (CPU).

    Args:
        img: PIL 图片对象.
        device: 可选 CUDA 设备对象, 为 ``None`` 时使用 numpy (CPU).

    Returns:
        np.ndarray: 归一化后的低频特征向量 (float32).

    Callers:
        - `dedupe.py:extract_fft_features`
    """
    if device is not None:
        import torch
        import torch.nn.functional as F

        t = torch.from_numpy(np.asarray(img.convert("RGB"), dtype=np.float32)).to(device)
        w = torch.tensor((0.299, 0.587, 0.114), dtype=t.dtype, device=device)
        gray = torch.einsum("hwc,c->hw", t, w)
        t = gray.view(1, 1, gray.shape[0], gray.shape[1])
        t = F.interpolate(t, size=(FFT_SIZE, FFT_SIZE), mode="bilinear", align_corners=False)
        x = t - t.mean()
        spec = torch.fft.fftshift(torch.fft.fft2(x, dim=(-2, -1)))
        mag = spec.abs()
        c = FFT_SIZE // 2
        half = FFT_LOW // 2
        feat = mag[:, :, c - half:c + half, c - half:c + half].reshape(-1)
        norm = feat.norm()
        feat = feat / torch.where(norm > 0, norm, torch.ones_like(norm))
        return feat.cpu().numpy().astype(np.float32)
    gray = img.convert("L")
    arr = np.asarray(gray, dtype=np.float32)
    arr = arr - arr.mean()
    spec = np.fft.fftshift(np.fft.fft2(arr))
    mag = np.abs(spec)
    c = FFT_SIZE // 2
    half = FFT_LOW // 2
    feat = mag[c - half:c + half, c - half:c + half].reshape(-1).astype(np.float32)
    norm = np.linalg.norm(feat)
    return feat / (norm if norm > 0 else 1.0)


def extract_fft_features(
    records: list[ImageRecord],
    *,
    progress=None,
    on_step: Callable[[int, int], None] | None = None,
) -> list[ImageRecord]:
    """为记录填充 FFT 低频特征向量.

    逐张读取原图并提取归一化低频幅度特征, 供基于余弦相似度的低频去重使用.
    降采样与 FFT 在 CUDA 上计算 (无 CUDA 时回退 numpy), 无需加载任何模型.

    Args:
        records: 已有指纹的记录列表.
        progress: 可选 ``ScoreProgress``, 更新 ``done`` / ``current``.
        on_step: 可选回调, 每张处理完成后以 ``(done, total)`` 调用, 供任务上报进度.

    Returns:
        list[ImageRecord]: 填充了 ``fft_feature`` 的记录列表.

    Callers:
        - `routes/dedupe.py:scan_duplicates`
    """
    device = cuda_device()
    total = len(records)
    for i, r in enumerate(records):
        try:
            with Image.open(r.path) as im:
                r.fft_feature = _fft_feature_one(im, device)
        except (OSError, UnidentifiedImageError):
            r.fft_feature = None
        if progress is not None:
            progress.done = i + 1
            progress.current = r.rel
        if on_step is not None:
            on_step(i + 1, total)
    return records


def extract_orb_features(
    records: list[ImageRecord],
    *,
    progress=None,
    on_step: Callable[[int, int], None] | None = None,
) -> list[ImageRecord]:
    """为记录填充 ORB 局部特征(压缩后灰度图的关键点描述子).

    每张图按最长边 ``ORB_MAX`` 等比压缩, 灰度化后提取不超过 ``ORB_NFEATURES``
    个 ORB 描述子, 供基于局部特征匹配的相似度去重使用. 纯 CPU 计算.

    Args:
        records: 已有指纹的记录列表.
        progress: 可选 ``ScoreProgress``, 更新 ``done`` / ``current``.
        on_step: 可选回调, 每张处理完成后以 ``(done, total)`` 调用, 供任务上报进度.

    Returns:
        list[ImageRecord]: 填充了 ``orb_des`` 的记录列表.

    Callers:
        - `routes/dedupe.py:scan_duplicates`
    """
    orb = cv2.ORB_create(nfeatures=ORB_NFEATURES)
    total = len(records)
    for i, r in enumerate(records):
        try:
            with Image.open(r.path) as im:
                gray = im.convert("L")
                gray.thumbnail((ORB_MAX, ORB_MAX))
                arr = np.asarray(gray, dtype=np.uint8)
                _, des = orb.detectAndCompute(arr, None)
                r.orb_des = des
        except (OSError, UnidentifiedImageError):
            r.orb_des = None
        if progress is not None:
            progress.done = i + 1
            progress.current = r.rel
        if on_step is not None:
            on_step(i + 1, total)
    return records


def collect_paths(data_dir: Path) -> list[Path]:
    """列出目录内图像路径. 引用: load_records. """
    data_dir = data_dir.resolve()
    out = []
    for p in sorted(data_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            out.append(p)
    return out


def load_records(data_dir: Path, workers: int | None = None) -> list[ImageRecord]:
    """并行加载去重指纹. 引用: ``routes/dedupe.py``. """
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


def _pairwise_dots_upper(
    mat: np.ndarray,
    device=None,
    block: int = 2048,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """计算全部上三角 (i<j) 特征点积, 在 ``device`` 上分块执行.

    每批取 ``block`` 行与整矩阵做一次矩阵乘法, 仅保留上三角部分, 避免
    生成完整 n×n 点积矩阵. ``device`` 为 CUDA 设备时用 torch gemm 加速,
    否则回退 numpy BLAS.

    Args:
        mat: 形状 (n, d) 的 float32 特征矩阵.
        device: 可选 CUDA 设备对象, 为 ``None`` 时用 numpy.
        block: 每次矩阵乘法的批行数.

    Returns:
        (rows, cols, dots): 上三角点积的压缩索引与值, 均为 float32/int64 数组.

    Callers:
        - `dedupe.py:find_duplicate_groups`
        - `dedupe.py:pairwise_values`
    """
    n = mat.shape[0]
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    if device is not None:
        import torch

        t = torch.from_numpy(np.ascontiguousarray(mat)).to(device)
        for i0 in range(0, n, block):
            i1 = min(i0 + block, n)
            sub = (t[i0:i1] @ t.T).cpu().numpy()
            for r in range(i1 - i0):
                i = i0 + r
                v = sub[r, i + 1:]
                if v.size:
                    rows.append(np.full(v.size, i, np.int64))
                    cols.append(np.arange(i + 1, n, dtype=np.int64))
                    vals.append(v.astype(np.float32))
    else:
        for i0 in range(0, n, block):
            i1 = min(i0 + block, n)
            sub = mat[i0:i1] @ mat.T
            for r in range(i1 - i0):
                i = i0 + r
                v = sub[r, i + 1:]
                if v.size:
                    rows.append(np.full(v.size, i, np.int64))
                    cols.append(np.arange(i + 1, n, dtype=np.int64))
                    vals.append(v.astype(np.float32))
    if not vals:
        return (
            np.empty(0, np.int64),
            np.empty(0, np.int64),
            np.empty(0, np.float32),
        )
    return (
        np.concatenate(rows),
        np.concatenate(cols),
        np.concatenate(vals),
    )


def _bits_ratio(b1: np.ndarray, b2: np.ndarray) -> float:
    """两个位矩阵之间的 ORB 匹配比例相似度.

    汉明距离由位计数与位点积推出: ``dist = cnt1 + cnt2 - 2·(b1 @ b2.T)``,
    其中点积用 BLAS 加速. 对每行取最小的两个距离做 Lowe 比率检验筛选
    可靠匹配, 返回可靠匹配数占两图较小描述子数的比例 (0~1, 越高越相似).

    Args:
        b1: 第一张图的位矩阵 (k1, 256) float32.
        b2: 第二张图的位矩阵 (k2, 256) float32.

    Returns:
        float: 匹配比例相似度.

    Callers:
        - `dedupe.py:_orb_similarity`
        - `dedupe.py:_compare_orb_edges`
        - `dedupe.py:pairwise_values`
    """
    same = b1 @ b2.T
    dist = b1.sum(1, keepdims=True) + b2.sum(1) - 2.0 * same
    two = np.partition(dist, 1, axis=1)[:, :2]
    d0 = two.min(axis=1)
    d1 = two.max(axis=1)
    good = int(np.count_nonzero(d0 < ORB_RATIO * d1))
    if good == 0:
        return 0.0
    return float(good) / float(min(len(b1), len(b2)))


def _orb_bits(des: np.ndarray | None) -> np.ndarray | None:
    """把 ORB 描述子展开为位矩阵 (k, 256) float32, 供汉明距离点积使用. """
    if des is None or len(des) < 2:
        return None
    return np.unpackbits(des, axis=1).astype(np.float32)


def _orb_similarity(des1: np.ndarray, des2: np.ndarray) -> float:
    """两张图 ORB 描述子的匹配比例相似度.

    以展开位矩阵 + BLAS 点积替代逐对 ``cv2.BFMatcher`` 计算汉明距离.

    Args:
        des1: 第一张图的 ORB 描述子 (k1, 32) uint8.
        des2: 第二张图的 ORB 描述子 (k2, 32) uint8.

    Returns:
        float: 匹配比例相似度.

    Callers:
        - `dedupe.py:_member_similarity`
    """
    b1 = _orb_bits(des1)
    b2 = _orb_bits(des2)
    if b1 is None or b2 is None:
        return 0.0
    return _bits_ratio(b1, b2)


def _compare_orb_edges(
    records: list[ImageRecord],
    pool: list[int],
    threshold: float,
) -> list[tuple[int, int]]:
    """ORB 匹配比例高于阈值的边. 引用: find_duplicate_groups.

    预处理将池内每图描述子展开为位矩阵一次, 之后两两比较仅做 BLAS 点积.
    """
    bits = [_orb_bits(records[p].orb_des) for p in pool]
    n = len(pool)
    thr = float(threshold)
    edges: list[tuple[int, int]] = []
    for i in range(n):
        b_i = bits[i]
        if b_i is None:
            continue
        for j in range(i + 1, n):
            b_j = bits[j]
            if b_j is None:
                continue
            if _bits_ratio(b_i, b_j) >= thr:
                edges.append((i, j))
    return edges


def pairwise_values(
    records: list[ImageRecord],
    algorithm: str,
) -> np.ndarray:
    """收集当前算法下全部两两相似度值(上三角, 不含自身).

    Args:
        records: 图像记录, 需已含对应算法所需特征.
        algorithm: 相似度算法, ``"mse"`` / ``"fft"`` / ``"orb"``.

    Returns:
        np.ndarray: 两两相似度值; mse 为 MSE 值, fft 为余弦, orb 为匹配比例.

    Callers:
        - `routes/dedupe.py:scan_duplicates`
    """
    if algorithm == "mse":
        n = len(records)
        fp = np.stack([r.fingerprint for r in records], axis=0)
        fp_flat = np.ascontiguousarray(fp.reshape(n, -1), dtype=np.float32)
        sq = np.sum(fp_flat * fp_flat, axis=1, dtype=np.float32)
        rows, cols, dots = _pairwise_dots_upper(fp_flat, cuda_device())
        mse = (sq[rows] + sq[cols] - 2.0 * dots) * (1.0 / float(fp_flat.shape[1]))
        return mse.astype(np.float32)
    if algorithm == "fft":
        feats = [r.fft_feature for r in records if r.fft_feature is not None]
        if not feats:
            return np.empty(0, np.float32)
        vec = np.ascontiguousarray(np.stack(feats), dtype=np.float32)
        _, _, dots = _pairwise_dots_upper(vec, cuda_device())
        return dots.astype(np.float32)
    bits = [_orb_bits(r.orb_des) for r in records]
    n = len(records)
    vals: list[float] = []
    for i in range(n):
        b_i = bits[i]
        if b_i is None:
            continue
        for j in range(i + 1, n):
            b_j = bits[j]
            if b_j is None:
                continue
            vals.append(_bits_ratio(b_i, b_j))
    return np.asarray(vals, dtype=np.float32)


def _member_similarity(
    records: list[ImageRecord],
    k: int,
    keep: int,
    algorithm: str,
) -> float | None:
    """组内成员与 keep 参照的相似度.

    - ``"fft"``: 归一化 FFT 低频特征的点积(余弦相似度, 0~1, 越高越相似).
    - ``"orb"``: ORB 描述子匹配比例(0~1, 越高越相似).
    - ``"mse"``: 指纹 MSE(越小越相似), keep 自身为 0.

    Args:
        records: 全部记录.
        k: 成员在 ``records`` 中的索引.
        keep: 参照(建议保留)记录索引.
        algorithm: 相似度算法.

    Returns:
        float | None: 相似度; 缺少特征时返回 None.

    Callers:
        - `dedupe.py:find_duplicate_groups`
    """
    if k == keep:
        return 1.0 if algorithm in ("fft", "orb") else 0.0
    if algorithm == "fft":
        fk = records[k].fft_feature
        fkp = records[keep].fft_feature
        if fk is None or fkp is None:
            return None
        return float(np.dot(fk, fkp))
    if algorithm == "orb":
        d1 = records[k].orb_des
        d2 = records[keep].orb_des
        if d1 is None or d2 is None:
            return None
        return _orb_similarity(d1, d2)
    fk = records[k].fingerprint.reshape(-1)
    fkp = records[keep].fingerprint.reshape(-1)
    return float(np.mean(np.square(fk - fkp)))


def suggest_keep_index(records: list[ImageRecord], idxs: list[int], similar_res_ratio: float) -> int:
    """组内建议保留索引. 引用: find_duplicate_groups. """
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


from services.models import DedupeGroup, DedupeMember


def find_duplicate_groups(
    records: list[ImageRecord],
    *,
    algorithm: str = "mse",
    mse_threshold: float = 1e-4,
    sim_threshold: float = 0.95,
    similar_res_ratio: float = 1.05,
    j_chunk: int = 2048,
) -> list[DedupeGroup]:
    """返回仅含重复的组；每组含 members / keep / suggested_remove.

    按 ``algorithm`` 选择相似度度量:

    - ``"mse"``: 逐像素指纹 MSE < ``mse_threshold`` 判为重复.
    - ``"fft"``: 图像 FFT 低频特征, 余弦相似度 >= ``sim_threshold`` 判为重复.
    - ``"orb"``: ORB 局部特征匹配比例 >= ``sim_threshold`` 判为重复.

    三种算法共用同一套并查集分组与"建议保留更清晰样本"策略.

    Args:
        records: 图像记录, 需含 ``fingerprint``(mse) 或 ``fft_feature``(fft) 或 ``orb_des``(orb).
        algorithm: 相似度算法, ``"mse"`` / ``"fft"`` / ``"orb"``.
        mse_threshold: MSE 阈值(仅 mse).
        sim_threshold: 相似度阈值(fft 余弦 / orb 匹配比例).
        similar_res_ratio: 组内分辨率是否相近的比例阈值.
        j_chunk: FFT/MSE 分块比较的列数.

    Returns:
        list[DedupeGroup]: 仅含重复的组列表, 按组大小降序.

    Raises:
        ValueError: ``algorithm`` 非法, 或对应模式缺少特征时抛出.

    引用:
        - ``routes/dedupe.py``.
    """
    n = len(records)
    if n < 2:
        return []

    device = cuda_device()
    if algorithm == "fft":
        good = [i for i, r in enumerate(records) if r.fft_feature is not None]
        if not good:
            raise ValueError("没有可用的 FFT 特征")
        feats = np.stack([cast(np.ndarray, records[i].fft_feature) for i in good], axis=0)
        vec = np.ascontiguousarray(feats, dtype=np.float32)
        rows, cols, dots = _pairwise_dots_upper(vec, device, block=j_chunk)
        mask = dots >= float(sim_threshold)
        edges = [(int(i), int(j)) for i, j in zip(rows[mask], cols[mask])]
        pool = good
    elif algorithm == "orb":
        good = [i for i, r in enumerate(records) if r.orb_des is not None and len(r.orb_des) >= 2]
        if not good:
            raise ValueError("没有可用的 ORB 特征")
        edges = _compare_orb_edges(records, good, float(sim_threshold))
        pool = good
    else:
        fp = np.stack([r.fingerprint for r in records], axis=0)
        fp_flat = np.ascontiguousarray(fp.reshape(n, -1), dtype=np.float32)
        sq = np.sum(fp_flat * fp_flat, axis=1, dtype=np.float32)
        rows, cols, dots = _pairwise_dots_upper(fp_flat, device, block=j_chunk)
        mse = (sq[rows] + sq[cols] - 2.0 * dots) * (1.0 / float(fp_flat.shape[1]))
        below = mse < float(mse_threshold)
        edges = [(int(i), int(j)) for i, j in zip(rows[below], cols[below])]
        pool = list(range(n))

    uf = UnionFind(len(pool))
    for i, j in edges:
        uf.union(i, j)

    buckets: dict[int, list[int]] = {}
    for i in range(len(pool)):
        buckets.setdefault(uf.find(i), []).append(i)

    groups: list[DedupeGroup] = []
    for idxs in buckets.values():
        if len(idxs) < 2:
            continue
        keep = suggest_keep_index(records, [pool[k] for k in idxs], similar_res_ratio)
        members: list[DedupeMember] = []
        for k in sorted([pool[x] for x in idxs], key=lambda x: records[x].rel):
            r = records[k]
            members.append(
                DedupeMember(
                    path=r.rel,
                    stem=Path(r.rel).stem,
                    width=r.width,
                    height=r.height,
                    blockiness=round(r.blockiness, 4),
                    area=r.width * r.height,
                    is_keep=k == keep,
                    similarity=_member_similarity(records, k, keep, algorithm),
                )
            )
        suggested_remove = [m.path for m in members if not m.is_keep]
        groups.append(
            DedupeGroup(
                keep=records[keep].rel,
                size=len(members),
                members=members,
                suggested_remove=suggested_remove,
            )
        )

    groups.sort(key=lambda g: (-g.size, g.keep))
    return groups
