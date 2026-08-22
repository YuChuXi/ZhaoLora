"""按数据集缓存的最近语义排序结果（持久化到磁盘）.

对「语义搜索排序」结果做短期记忆：以查询文本为键，保存最近
``MAX_ENTRIES`` 次排序的命中列表，同一数据集内重复查询可直接复用，
避免重复调用 Qwen3-VL-Reranker-2B 模型；同时提供最近查询历史，
供前端语义搜索对话框下拉展示。

缓存持久化为数据集根目录下的 ``.semantic_sort_cache.json``，每数据集
一份；写入采用先写临时文件再替换的原子操作，避免半写损坏；服务重启后
由 ``SemanticSortCache.__init__`` 自动加载恢复，保证跨会话保留。

Callers:
    - ``services/dataset/runtime.py:`` : DatasetRegistry 按数据集构建实例
    - ``routes/tag_stats.py:`` : embedding_query / embedding_history
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 每个数据集最多保留的排序结果次数
MAX_ENTRIES = 20
# 缓存文件名 (置于数据集根目录)
CACHE_FILENAME = ".semantic_sort_cache.json"
# 磁盘格式版本; 不匹配时视为旧缓存, 重建
_VERSION = 1

# 内存条目: (写入时间戳, 目录名集合, [(相对路径, 相似度), ...])
_Entry = tuple[float, tuple[str, ...], list[tuple[str, float]]]


class SemanticSortCache:
    """保存某数据集最近若干次语义排序结果, 持久化到数据集根目录.

    以查询文本为键；条目与执行时选中的目录集合绑定，目录变化视为未命中，
    避免跨目录复用过期结果。命中条目同时登记到最近列表，供前端下拉展示。
    每次写入即原子持久化，重启后由 ``_load`` 恢复。
    """

    def __init__(self, path: Path) -> None:
        """绑定缓存文件路径并从磁盘加载已有结果.

        Args:
            path (Path): 缓存文件路径 (数据集根目录下).

        Callers:
            - ``services/dataset/runtime.py:DatasetRegistry.ensure``
        """
        self._path = Path(path)
        self._entries: dict[str, _Entry] = {}
        self._recent: list[str] = []
        self._load()

    def _load(self) -> None:
        """从磁盘加载持久化结果; 文件缺失或版本不符时视为空缓存.

        解析与项目分数文件一致: JSON 语法或结构错误直接抛出，不静默吞掉；
        仅「文件不存在」与「版本不匹配」两种情况返回空缓存，待下次写入重建。

        Callers:
            - ``services/analysis/sort_cache.py:SemanticSortCache.__init__``
        """
        if not self._path.is_file():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return
        entries = raw.get("entries")
        recent = raw.get("recent")
        if not isinstance(entries, dict) or not isinstance(recent, list):
            raise ValueError("语义排序缓存结构无效")
        for query, entry in entries.items():
            parsed = self._coerce(entry)
            if isinstance(query, str) and query and parsed is not None:
                self._entries[query] = parsed
        self._recent = [q for q in recent if isinstance(q, str) and q in self._entries]

    def _save(self) -> None:
        """原子写盘: 先写临时文件再替换, 避免进程中断导致半写损坏.

        Callers:
            - ``services/analysis/sort_cache.py:SemanticSortCache.put``
        """
        payload = {
            "version": _VERSION,
            "entries": {
                query: {"ts": entry[0], "dirs": list(entry[1]), "hits": [list(h) for h in entry[2]]}
                for query, entry in self._entries.items()
            },
            "recent": self._recent,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _coerce(entry: object) -> _Entry | None:
        """把磁盘 JSON 条目规整为内存结构; 无效条目返回 None 丢弃.

        Args:
            entry (object): 从 JSON 解析出的条目节点。

        Returns:
            _Entry | None: 规整后的条目；结构不符时返回 None。

        Callers:
            - ``services/analysis/sort_cache.py:SemanticSortCache._load``
        """
        if not isinstance(entry, dict):
            return None
        ts = entry.get("ts")
        dirs = entry.get("dirs")
        hits = entry.get("hits")
        if not isinstance(ts, (int, float)):
            return None
        if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
            return None
        if not isinstance(hits, list):
            return None
        parsed: list[tuple[str, float]] = []
        for hit in hits:
            if (
                isinstance(hit, list) and len(hit) == 2
                and isinstance(hit[0], str)
                and isinstance(hit[1], (int, float))
            ):
                parsed.append((hit[0], float(hit[1])))
        return float(ts), tuple(dirs), parsed

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
            - ``routes/tag_stats.py:embedding_query``
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

        写入后立即原子持久化，保证重启后仍可恢复。

        Args:
            query (str): 查询文本。
            dirs (tuple[str, ...]): 排序时选中的目录名集合（已排序）。
            hits (list[tuple[str, float]]): 排序结果 ``[(相对路径, 相似度), ...]``。

        Callers:
            - ``routes/tag_stats.py:embedding_query``
        """
        self._entries[query] = (time.time(), dirs, hits)
        if query in self._recent:
            self._recent.remove(query)
        self._recent.insert(0, query)
        for stale in self._recent[MAX_ENTRIES:]:
            self._entries.pop(stale, None)
        del self._recent[MAX_ENTRIES:]
        self._save()

    def recent(self) -> list[tuple[str, float]]:
        """返回最近查询历史，新的在前.

        Returns:
            list[tuple[str, float]]: ``[(查询文本, 时间戳), ...]``，
                至多 ``MAX_ENTRIES`` 条。

        Callers:
            - ``routes/tag_stats.py:embedding_history``
        """
        return [(q, self._entries[q][0]) for q in self._recent]
