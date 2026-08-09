"""Qwen3-VL-Embedding-2B 多模态向量嵌入服务.

使用 ``sentence_transformers`` 加载模型，为图片和文本生成统一向量表示。
嵌入结果缓存在内存中，并通过 npz 文件持久化到磁盘。

Callers:
    - ``routes/tag_stats``: 向量构建、查询、批量标记 API
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
from PIL import Image

from services.core.device import cuda_device

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

Image.MAX_IMAGE_PIXELS = None

_MODEL_NAME = "Qwen/Qwen3-VL-Embedding-2B"

_EMBED_MODEL: SentenceTransformer | None = None
_EMBED_LOCK = threading.Lock()


def model_ready() -> tuple[bool, str]:
    """检查模型是否已加载，未加载时从 HuggingFace 加载。

    ``huggingface_hub`` 下载模型时遵循 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量。

    Returns:
        ``(ok, message)``. ok 为 True 时表示模型可用。

    Callers:
        - ``routes/tag_stats``
    """
    global _EMBED_MODEL

    if _EMBED_MODEL is not None:
        return True, "模型已加载"

    with _EMBED_LOCK:
        if _EMBED_MODEL is not None:
            return True, "模型已加载"
        try:
            from sentence_transformers import SentenceTransformer

            device = cuda_device()
            model_kwargs: dict = {}
            if device is not None:
                model_kwargs["device"] = str(device)

            _EMBED_MODEL = SentenceTransformer(
                _MODEL_NAME,
                trust_remote_code=True,
                **model_kwargs,
            )
            return True, f"模型加载成功: {_MODEL_NAME}"
        except Exception as e:
            return False, f"模型加载失败: {e}"


def _get_model() -> SentenceTransformer:
    """获取已加载的模型（确保已调用 ``model_ready``）。

    Returns:
        SentenceTransformer 实例。

    Callers:
        - ``embed_image``
        - ``embed_text``
    """
    return cast("SentenceTransformer", _EMBED_MODEL)


def embed_image(image_path: str | Path) -> np.ndarray | None:
    """对单张图片计算向量嵌入。

    Args:
        image_path: 图片文件路径。

    Returns:
        np.ndarray: 归一化嵌入向量，解码失败时返回 None。

    Callers:
        - ``EmbeddingIndex.ensure_many``
        - ``EmbeddingIndex.embed_for``
    """
    path = Path(image_path)
    if not path.is_file():
        return None
    try:
        img = Image.open(path).convert("RGB")
        model = _get_model()
        vec = model.encode(img)
        return np.asarray(vec, dtype=np.float32)
    except Exception:
        return None


def embed_text(text: str) -> np.ndarray:
    """对查询文本计算向量嵌入。

    Args:
        text: 查询文本字符串。

    Returns:
        np.ndarray: 归一化嵌入向量。

    Callers:
        - ``EmbeddingIndex.query``
        - ``routes/tag_stats``
    """
    model = _get_model()
    vec = model.encode(text)
    return np.asarray(vec, dtype=np.float32)


# 缓存文件名
_CACHE_NAME = "embedding_cache.npz"


class EmbeddingIndex:
    """图片向量嵌入索引，支持内存缓存与磁盘持久化。

    线程安全。嵌入按图片相对路径索引，缓存文件时间戳用于失效判定。

    Callers:
        - ``routes/tag_stats``
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        """初始化嵌入索引。

        Args:
            cache_dir: npz 缓存目录；None 表示仅内存模式。

        Callers:
            - ``routes/tag_stats``
        """
        self._lock = threading.RLock()
        self._embeddings: dict[str, tuple[float, np.ndarray]] = {}
        self._cache_path = (cache_dir / _CACHE_NAME) if cache_dir else None
        self._dirty = False
        if self._cache_path:
            self._load_disk()

    def set_cache_dir(self, cache_dir: Path) -> None:
        """设置或更改磁盘缓存目录。

        Args:
            cache_dir: npz 缓存所在目录。

        Callers:
            - ``routes/tag_stats``
        """
        with self._lock:
            self._cache_path = cache_dir / _CACHE_NAME
            cache_dir.mkdir(parents=True, exist_ok=True)
            if not self._embeddings:
                self._load_disk()

    def clear(self) -> None:
        """清空内存中的全部嵌入。

        Callers:
            - ``routes/tag_stats``
        """
        with self._lock:
            self._embeddings.clear()
            self._dirty = True

    def is_empty(self) -> bool:
        """检查索引是否为空。

        Returns:
            bool: True 表示无任何嵌入数据。

        Callers:
            - ``routes/tag_stats``
        """
        with self._lock:
            return len(self._embeddings) == 0

    def put(self, rel: str, mtime: float, vec: np.ndarray) -> None:
        """写入或更新单条嵌入。

        Args:
            rel: 图片相对路径。
            mtime: 文件修改时间（用于缓存失效判定）。
            vec: 嵌入向量。

        Callers:
            - ``EmbeddingIndex.embed_for``
            - ``EmbeddingIndex.ensure_many``
        """
        with self._lock:
            self._embeddings[rel] = (mtime, np.asarray(vec, dtype=np.float32))
            self._dirty = True

    def _load_disk(self) -> None:
        """从 npz 文件加载缓存到内存。

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
            self._embeddings[str(rel)] = (
                float(mtimes[i]),
                np.asarray(matrix[i], dtype=np.float32),
            )

    def flush(self) -> None:
        """将内存中的嵌入原子写入 npz 文件（仅在 dirty 时生效）。

        Callers:
            - ``ensure_many``
        """
        with self._lock:
            if not self._dirty or not self._cache_path:
                return
            if not self._embeddings:
                return
            rels = list(self._embeddings.keys())
            mtimes = np.array([self._embeddings[r][0] for r in rels], dtype=np.float64)
            matrix = np.stack([self._embeddings[r][1] for r in rels], axis=0)
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp.npz")
            np.savez_compressed(
                tmp,
                rels=np.array(rels, dtype=object),
                mtimes=mtimes,
                matrix=matrix,
            )
            tmp.replace(self._cache_path)
            self._dirty = False

    def embed_for(self, data_dir: Path, rel: str) -> np.ndarray | None:
        """取单个相对路径的图片嵌入（从缓存或重新计算）。

        mtime 变化时自动重新计算。

        Args:
            data_dir: 数据集根目录。
            rel: 相对路径。

        Returns:
            np.ndarray | None: 嵌入向量，或文件缺失/解码失败时为 None。

        Callers:
            - ``ensure_many``
        """
        path = data_dir / rel
        if not path.is_file():
            with self._lock:
                self._embeddings.pop(rel, None)
            return None
        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._embeddings.get(rel)
            if cached and cached[0] == mtime:
                return cached[1]

        vec = embed_image(path)
        if vec is None:
            with self._lock:
                self._embeddings.pop(rel, None)
            return None
        self.put(rel, mtime, vec)
        return vec

    def ensure_many(
        self,
        data_dir: Path,
        rels: list[str],
    ) -> None:
        """批量确保候选图片的嵌入已加载到内存（并行计算缺失项，完成后写入磁盘）。

        Args:
            data_dir: 数据集根目录。
            rels: 图片相对路径列表。

        Callers:
            - ``routes/tag_stats``
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        missing: list[str] = []
        with self._lock:
            for rel in rels:
                path = data_dir / rel
                if not path.is_file():
                    continue
                mtime = path.stat().st_mtime
                cached = self._embeddings.get(rel)
                if not cached or cached[0] != mtime:
                    missing.append(rel)

        if not missing:
            return

        def one(rel: str) -> tuple[str, float, np.ndarray] | None:
            """为单张图计算嵌入。引用: ensure_many。"""
            path = data_dir / rel
            if not path.is_file():
                return None
            mtime = path.stat().st_mtime
            vec = embed_image(path)
            if vec is None:
                return None
            return rel, mtime, vec

        with ThreadPoolExecutor(max_workers=1) as pool:
            futs = [pool.submit(one, rel) for rel in missing]
            for fut in as_completed(futs):
                item = fut.result()
                if item is None:
                    continue
                rel, mtime, vec = item
                self.put(rel, mtime, vec)
        self.flush()

    def query(
        self,
        query_vec: np.ndarray,
        candidates: list[str],
        top_k: int = 0,
    ) -> list[tuple[str, float]]:
        """按余弦相似度查询最近的候选。

        Args:
            query_vec: 查询向量（已归一化）。
            candidates: 候选图片相对路径列表。
            top_k: 返回前 k 个结果；0 或负数表示返回全部。

        Returns:
            ``[(相对路径, 余弦相似度), ...]``，按相似度降序排列。

        Callers:
            - ``routes/tag_stats``
        """
        with self._lock:
            rows: list[str] = []
            vecs: list[np.ndarray] = []
            for rel in candidates:
                cached = self._embeddings.get(rel)
                if cached is None:
                    continue
                rows.append(rel)
                vecs.append(cached[1])

        if not rows:
            return []

        mat = np.stack(vecs, axis=0)
        sims = mat @ query_vec.reshape(-1, 1)
        sims = sims.flatten()

        if top_k > 0 and top_k < len(rows):
            indices = np.argpartition(sims, -top_k)[-top_k:]
            indices = indices[np.argsort(-sims[indices])]
        else:
            indices = np.argsort(-sims)

        return [(rows[i], float(sims[i])) for i in indices]