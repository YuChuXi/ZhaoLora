"""按数据集缓存的最近语义排序结果（内存驻留）.

对「语义搜索排序」结果做短期记忆：以查询文本为键，保存最近
``MAX_ENTRIES`` 次排序的命中列表，同一数据集内重复查询可直接复用，
避免重复调用 Qwen3-VL-Reranker-2B 模型；同时提供最近查询历史，
供前端语义搜索对话框下拉展示。

Callers:
    - ``services/dataset/runtime.py``: DatasetRuntime 持有缓存实例
    - ``routes/tag_stats.py``: embedding_query / embedding_history
"""

from __future__ import annotations

import time

# 每个数据集最多保留的排序结果次数
MAX_ENTRIES = 20


class SemanticSortCache:
    """保存某数据集最近若干次语义排序结果的内存缓存.

    以查询文本为键；条目与执行时选中的目录集合绑定，目录变化视为未命中，
    避免跨目录复用过期结果。命中条目同时登记到最近列表，供前端下拉展示。
    """

    def __init__(self) -> None:
        """初始化空缓存. 引用: ``DatasetRuntime`` 字段默认工厂. """
        self._entries: dict[
            str, tuple[float, tuple[str, ...], list[tuple[str, float]]]
        ] = {}
        self._recent: list[str] = []

    def get(self, query: str, dirs: tuple[str, ...]) -> list[tuple[str, float]] | None:
        """按查询文本与目录取排序结果; 未命中或目录不一致返回 None.

        命中时将该查询提升到最近列表最前，避免被后续新查询挤出缓存。

        Args:
            query (str): 查询文本。
            dirs (tuple[str, ...]): 排序时选中的目录名集合（已排序），校验作用域。

        Returns:
            list[tuple[str, float]] | None: 命中的排序结果
                ``[(相对路径, 相似度), ...]``，按相似度降序；未命中返回 None。

        Callers:
            - ``routes/tag_stats:embedding_query``
        """
        entry = self._entries.get(query)
        if entry is None:
            return None
        _, cached_dirs, hits = entry
        if cached_dirs != dirs:
            return None
        if self._recent and self._recent[0] != query:
            self._recent.remove(query)
            self._recent.insert(0, query)
        return hits

    def put(self, query: str, dirs: tuple[str, ...], hits: list[tuple[str, float]]) -> None:
        """写入一次排序结果并登记为最近查询，超出上限时淘汰最旧条目.

        Args:
            query (str): 查询文本。
            dirs (tuple[str, ...]): 排序时选中的目录名集合（已排序）。
            hits (list[tuple[str, float]]): 排序结果 ``[(相对路径, 相似度), ...]``。

        Callers:
            - ``routes/tag_stats:embedding_query``
        """
        self._entries[query] = (time.time(), dirs, hits)
        if query in self._recent:
            self._recent.remove(query)
        self._recent.insert(0, query)
        for stale in self._recent[MAX_ENTRIES:]:
            self._entries.pop(stale, None)
        del self._recent[MAX_ENTRIES:]

    def recent(self) -> list[tuple[str, float]]:
        """返回最近查询历史，新的在前.

        Returns:
            list[tuple[str, float]]: ``[(查询文本, 时间戳), ...]``，
                至多 ``MAX_ENTRIES`` 条。

        Callers:
            - ``routes/tag_stats:embedding_history``
        """
        return [(q, self._entries[q][0]) for q in self._recent]
