"""在线源缩略图缓存.

全部条目存放在任务根下的单个 SQLite 文件中, 键为 URL 摘要, 值为内容类型与
原始字节; 持久化经 SQLAlchemy 异步 ORM. 条目顺序由内存中的 W-TinyLFU 维护:
新条目进入窗口段, 窗口溢出时与主区候选按访问频率准入比较, 主区分试用段与
保护段, 命中提升到保护段。容量同时受条目数与总字节数约束, 超限时优先淘汰
低频条目, 其次最久未使用。

配置经环境变量提供: ``ONLINE_THUMB_CACHE_ITEMS`` 为条目数上限,
``ONLINE_THUMB_CACHE_MB`` 为总体积上限 (MB); 条目数为 0 时禁用缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from sqlalchemy import String, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from services.core.db import Database

# 频率草图深度: 每个键取 4 组独立散列, 估计值取四者最小值.
_SKETCH_DEPTH = 4
# 4 位计数器饱和值, 防止单个高频键长期压制其他键.
_SKETCH_MAX = 15
# 每累计写入这么多次访问就整体减半, 使频率估计跟随近期访问.
_SKETCH_RESET_FACTOR = 10
# 保护段占主区的比例 (五分之四), 其余为试用段.
_PROTECTED_PERCENT = 4

# 拉取回调: 返回内容类型与字节迭代器, 由来源客户端按图床策略实现.
ProduceFn = Callable[[], Awaitable[tuple[str, AsyncIterator[bytes]]]]


class ThumbBase(DeclarativeBase):
    """缩略图缓存库的声明基类.

    Callers:
        - ``services/online_sources/framework/thumb_cache.py:ThumbRow``
        - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start``
    """


class ThumbRow(ThumbBase):
    """缩略图缓存索引条目.

    ``id`` 提供插入顺序, 缓存重启时据此重建条目顺序; ``key`` 为 URL 摘要;
    图片字节存于文件系统, 本表只记录内容类型与字节数.

    Callers:
        - ``services/online_sources/framework/thumb_cache.py:ThumbCache``
    """

    __tablename__ = "thumbs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ctype: Mapped[str] = mapped_column(String(128))
    size: Mapped[int]


class _FrequencySketch:
    """Count-Min Sketch: 以 4 位计数器估计键的访问频率.

    Attributes:
        width (int): 每组散列的槽位数, 取不小于条目上限的 2 的幂.
    """

    def __init__(self, capacity: int) -> None:
        """按容量上限分配计数器表.

        Args:
            capacity (int): 条目数上限, 决定散列宽度.
        """
        width = 1
        while width < capacity:
            width <<= 1
        self.width = width
        self._table = bytearray(_SKETCH_DEPTH * width)
        self._mask = width - 1
        self._additions = 0
        self._sample_size = max(10, capacity * _SKETCH_RESET_FACTOR)

    def increment(self, key: str) -> None:
        """记录一次访问, 累计到采样窗口后整体减半.

        Args:
            key (str): 缓存键.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu.hit``
        """
        for offset in self._slots(key):
            if self._table[offset] < _SKETCH_MAX:
                self._table[offset] += 1
        self._additions += 1
        if self._additions >= self._sample_size:
            self._halve()

    def estimate(self, key: str) -> int:
        """取四组计数器的最小值作为频率估计.

        Args:
            key (str): 缓存键.

        Returns:
            int: 频率估计值, 0 表示从未访问.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._admit``
        """
        return min(self._table[offset] for offset in self._slots(key))

    def _slots(self, key: str) -> list[int]:
        """给出该键在四组散列中的槽位偏移.

        Args:
            key (str): 缓存键.

        Returns:
            list[int]: 四组计数器在表中的绝对偏移.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_FrequencySketch.increment``
            - ``services/online_sources/framework/thumb_cache.py:_FrequencySketch.estimate``
        """
        digest = hashlib.blake2b(key.encode("utf-8"), digest_size=4 * _SKETCH_DEPTH).digest()
        return [
            group * self.width
            + (int.from_bytes(digest[group * 4 : group * 4 + 4], "big") & self._mask)
            for group in range(_SKETCH_DEPTH)
        ]

    def _halve(self) -> None:
        """把所有计数器右移一位并清零采样计数.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_FrequencySketch.increment``
        """
        self._table = bytearray(value >> 1 for value in self._table)
        self._additions = 0


class _Segment:
    """按字节记账的 LRU 段.

    Attributes:
        order (OrderedDict[str, int]): 键到字节数的有序映射, 末尾为最近使用.
        bytes (int): 本段条目占用字节总数.
    """

    def __init__(self) -> None:
        """创建空段."""
        self.order: OrderedDict[str, int] = OrderedDict()
        self.bytes = 0

    def add(self, key: str, size: int, *, recent: bool) -> None:
        """把条目放入段尾(最近使用)或段首(最久未使用).

        Args:
            key (str): 缓存键.
            size (int): 条目字节数.
            recent (bool): 为真放段尾, 否则放段首.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu.insert``
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu.hit``
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._admit``
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._demote``
        """
        self.order[key] = size
        if not recent:
            self.order.move_to_end(key, last=False)
        self.bytes += size

    def take(self, key: str) -> int:
        """移除指定条目并返回其字节数.

        Args:
            key (str): 缓存键.

        Returns:
            int: 被移除条目的字节数.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._release``
        """
        size = self.order.pop(key)
        self.bytes -= size
        return size

    def pop_oldest(self) -> tuple[str, int]:
        """移除并返回段首条目.

        Returns:
            tuple[str, int]: 键与字节数.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._admit``
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._evict_one``
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._demote``
        """
        key, size = self.order.popitem(last=False)
        self.bytes -= size
        return key, size

    def oldest(self) -> str | None:
        """返回段首键而不移除.

        Returns:
            str | None: 最久未使用的键; 段为空时为 None.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._admit``
        """
        for key in self.order:
            return key
        return None


class _TinyLfu:
    """W-TinyLFU 条目顺序: 窗口段 + 试用段 + 保护段.

    Attributes:
        max_bytes (int): 总字节上限; 0 表示不限.
        max_items (int): 条目数上限.
        sizes (dict[str, int]): 键到字节数.
    """

    def __init__(self, max_bytes: int, max_items: int) -> None:
        """按容量上限分段.

        Args:
            max_bytes (int): 总字节上限, 0 表示不限.
            max_items (int): 条目数上限.
        """
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.sizes: dict[str, int] = {}
        self.bytes = 0
        self.window = _Segment()
        self.probation = _Segment()
        self.protected = _Segment()
        self.sketch = _FrequencySketch(max(1, max_items))
        self._window_limit = max(1, max_bytes // 100) if max_bytes > 0 else 0

    def insert(self, key: str, size: int) -> list[str]:
        """加入新条目并返回需从存储删除的键.

        Args:
            key (str): 缓存键.
            size (int): 条目字节数.

        Returns:
            list[str]: 因容量或准入比较被淘汰的键.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.fetch``
        """
        self.sizes[key] = size
        self.bytes += size
        self.window.add(key, size, recent=True)
        return self._drain()

    def hit(self, key: str) -> list[str]:
        """记录命中并把条目提升到保护段.

        Args:
            key (str): 缓存键.

        Returns:
            list[str]: 因此被淘汰的键.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.fetch``
        """
        self.sketch.increment(key)
        if key in self.window.order:
            self.window.order.move_to_end(key)
        elif key in self.probation.order:
            self.probation.take(key)
            self.protected.add(key, self.sizes[key], recent=True)
        elif key in self.protected.order:
            self.protected.order.move_to_end(key)
        self._demote()
        return self._drain()

    def load(self, key: str, size: int) -> None:
        """从持久化存储恢复条目, 统一放入试用段.

        Args:
            key (str): 缓存键.
            size (int): 条目字节数.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start``
        """
        self.sizes[key] = size
        self.bytes += size
        self.probation.add(key, size, recent=True)

    def drain(self) -> list[str]:
        """按容量上限淘汰条目.

        Returns:
            list[str]: 被淘汰的键.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start``
        """
        return self._drain()

    def _drain(self) -> list[str]:
        """先挤出窗口段候选, 再按试用段与保护段顺序淘汰.

        Returns:
            list[str]: 被淘汰的键.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu.insert``
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu.hit``
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu.drain``
        """
        victims: list[str] = []
        if self._window_limit > 0:
            while self.window.bytes > self._window_limit and self.window.order:
                candidate, _ = self.window.pop_oldest()
                victims.extend(self._admit(candidate))
        while (self.max_bytes > 0 and self.bytes > self.max_bytes) or len(
            self.sizes
        ) > self.max_items:
            if not (self.probation.order or self.protected.order or self.window.order):
                break
            victims.extend(self._evict_one())
        return victims

    def _admit(self, candidate: str) -> list[str]:
        """把窗口段候选与试用段受害者按频率比较后准入.

        Args:
            candidate (str): 被窗口段挤出的键.

        Returns:
            list[str]: 被淘汰的键.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._drain``
        """
        victim = self.probation.oldest()
        if victim is None:
            self.probation.add(candidate, self.sizes[candidate], recent=True)
            return []
        if self.sketch.estimate(candidate) > self.sketch.estimate(victim):
            self.probation.take(victim)
            self.probation.add(candidate, self.sizes[candidate], recent=True)
            return self._release(victim)
        return self._release(candidate)

    def _demote(self) -> None:
        """把保护段溢出部分的段首条目降级到试用段.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu.hit``
        """
        limit = self.max_bytes * _PROTECTED_PERCENT // 5 if self.max_bytes > 0 else 0
        while limit > 0 and self.protected.bytes > limit and self.protected.order:
            key, size = self.protected.pop_oldest()
            self.probation.add(key, size, recent=False)

    def _evict_one(self) -> list[str]:
        """从试用段取最久未使用条目淘汰, 段空时依次取保护段与窗口段.

        Returns:
            list[str]: 含单个被淘汰键的列表.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._drain``
        """
        for segment in (self.probation, self.protected, self.window):
            if segment.order:
                key, _ = segment.pop_oldest()
                return self._release(key)
        return []

    def _release(self, key: str) -> list[str]:
        """从容量记账中移除条目.

        Args:
            key (str): 缓存键.

        Returns:
            list[str]: 含被移除键的列表, 供调用方删除存储条目.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._admit``
            - ``services/online_sources/framework/thumb_cache.py:_TinyLfu._evict_one``
        """
        size = self.sizes.pop(key)
        self.bytes -= size
        return [key]


class ThumbCache:
    """在线源缩略图缓存: 索引入库, 字节存文件.

    Attributes:
        db (Database): 索引库的异步引擎与会话工厂.
        root (Path): 图片字节根目录, 按摘要前两位分片存放.
        max_items (int): 条目数上限; 0 表示禁用缓存.
        max_bytes (int): 总字节数上限.
    """

    def __init__(self, db: Database, root: Path) -> None:
        """读取容量配置并绑定索引库与文件根, 不触碰磁盘.

        Args:
            db (Database): 索引库句柄, 由 ``server.py:lifespan`` 装配.
            root (Path): 图片字节根目录, 通常为任务根下的 ``.cache/thumb_files``.
        """
        self.db = db
        self.root = root
        self.max_items = _env_int("ONLINE_THUMB_CACHE_ITEMS", 2048)
        self.max_bytes = _env_int("ONLINE_THUMB_CACHE_MB", 2048) * 1024 * 1024
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._policy = _TinyLfu(self.max_bytes, max(1, self.max_items))
        self._write_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """缓存是否启用.

        Returns:
            bool: 条目数上限大于 0 时返回 True.

        Callers:
            - ``routes/online_sources.py:proxy_image``
        """
        return self.max_items > 0

    async def start(self) -> None:
        """建表并核对索引与文件, 再按索引重建容量策略.

        索引有行而文件缺失的条目直接删行; 文件存在而索引无行的视为上次写入
        中断的残留, 直接删除, 使运行期的索引与文件始终一致.

        Returns:
            None: 无返回值.

        Callers:
            - ``server.py:lifespan``
        """
        await self.db.create_all(ThumbBase)
        if not self.enabled:
            return
        rows = await self._load_index()
        stale, orphans = await asyncio.to_thread(
            self._reconcile, [key for key, _ in rows]
        )
        await self._delete_rows(stale)
        await asyncio.to_thread(self._remove_files, orphans)
        dropped = set(stale)
        for key, size in rows:
            if key not in dropped:
                self._policy.load(key, size)
        await self._apply(self._policy.drain())

    async def close(self) -> None:
        """释放数据库引擎与连接池.

        Returns:
            None: 无返回值.

        Callers:
            - ``server.py:lifespan``
        """
        await self.db.close()

    async def fetch(self, url: str, produce: ProduceFn) -> tuple[bytes, str, bool]:
        """取缩略图: 命中返回缓存字节, 未命中拉取后写入.

        Args:
            url (str): 图床缩略图地址.
            produce (ProduceFn): 未命中时的拉取回调, 返回内容类型与字节迭代器.

        Returns:
            tuple[bytes, str, bool]: 缩略图字节、内容类型、是否命中.

        Raises:
            ValueError: 上游地址非法或不属于该来源图床.
            NetworkError: 上游网络或状态错误.

        Callers:
            - ``routes/online_sources.py:proxy_image``
        """
        hit = await self._load(url)
        if hit is not None:
            await self._apply(self._policy.hit(url))
            return hit[0], hit[1], True
        lock = self._locks.get(url)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[url] = lock
        async with lock:
            hit = await self._load(url)
            if hit is not None:
                await self._apply(self._policy.hit(url))
                return hit[0], hit[1], True
            ctype, chunks = await produce()
            data = b"".join([chunk async for chunk in chunks])
            victims = self._policy.insert(url, len(data))
            if url in self._policy.sizes:
                await self._save(url, ctype, data)
            await self._apply(victims)
            return data, ctype, False

    async def _load_index(self) -> list[tuple[str, int]]:
        """读取全部条目的键与字节数, 按写入顺序排列.

        Returns:
            list[tuple[str, int]]: 键与字节数.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start``
        """
        sessions = self.db.sessions
        async with sessions() as session:
            result = await session.execute(
                select(ThumbRow.key, ThumbRow.size).order_by(ThumbRow.id)
            )
            return [(row.key, row.size) for row in result.all()]

    async def _load(self, url: str) -> tuple[bytes, str] | None:
        """按 URL 读取缓存条目.

        Args:
            url (str): 图床缩略图地址.

        Returns:
            tuple[bytes, str] | None: 命中时为字节与内容类型, 否则为 None.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.fetch``
        """
        sessions = self.db.sessions
        async with sessions() as session:
            result = await session.execute(
                select(ThumbRow.ctype).where(ThumbRow.key == self._key(url))
            )
            row = result.first()
        if row is None:
            return None
        data = await asyncio.to_thread(self._read_file, self._path(self._key(url)))
        return data, row.ctype

    async def _save(self, url: str, ctype: str, data: bytes) -> None:
        """写入图片字节并登记索引.

        Args:
            url (str): 图床缩略图地址.
            ctype (str): 内容类型.
            data (bytes): 缩略图字节.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.fetch``
        """
        key = self._key(url)
        await asyncio.to_thread(self._write_file, self._path(key), data)
        sessions = self.db.sessions
        statement = sqlite_insert(ThumbRow).values(
            key=key, ctype=ctype, size=len(data)
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ThumbRow.key],
            set_={"ctype": ctype, "size": len(data)},
        )
        async with self._write_lock, sessions() as session:
            await session.execute(statement)
            await session.commit()

    async def _apply(self, victims: list[str]) -> None:
        """删除被淘汰条目的索引与文件.

        Args:
            victims (list[str]): 被淘汰的键.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start``
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.fetch``
        """
        if not victims:
            return
        keys = [self._key(url) for url in victims]
        await self._delete_rows(keys)
        await asyncio.to_thread(self._remove_files, keys)

    async def _delete_rows(self, keys: list[str]) -> None:
        """删除索引记录, 不触碰文件.

        Args:
            keys (list[str]): 缓存键.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start``
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._apply``
        """
        if not keys:
            return
        sessions = self.db.sessions
        async with self._write_lock, sessions() as session:
            await session.execute(delete(ThumbRow).where(ThumbRow.key.in_(keys)))
            await session.commit()

    def _path(self, key: str) -> Path:
        """按摘要前两位分片给出图片文件路径.

        Args:
            key (str): 缓存键.

        Returns:
            Path: 图片文件路径.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._load``
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._save``
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._remove_files``
        """
        return self.root / key[:2] / key

    def _read_file(self, path: Path) -> bytes:
        """读取图片文件.

        Args:
            path (Path): 图片文件路径.

        Returns:
            bytes: 图片字节.

        Raises:
            FileNotFoundError: 索引存在而文件缺失; 启动核对保证运行期不会出现.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._load`` (经 ``asyncio.to_thread``)
        """
        return path.read_bytes()

    def _write_file(self, path: Path, data: bytes) -> None:
        """原子写入图片文件.

        Args:
            path (Path): 图片文件路径.
            data (bytes): 图片字节.

        Returns:
            None: 无返回值.

        Raises:
            OSError: 目录不可写.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._save`` (经 ``asyncio.to_thread``)
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _remove_files(self, keys: list[str]) -> None:
        """删除图片文件.

        Args:
            keys (list[str]): 缓存键.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start`` (经 ``asyncio.to_thread``)
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._apply`` (经 ``asyncio.to_thread``)
        """
        for key in keys:
            self._path(key).unlink(missing_ok=True)

    def _reconcile(self, keys: list[str]) -> tuple[list[str], list[str]]:
        """核对索引与文件, 给出缺失文件的索引键与无索引的残留文件键.

        Args:
            keys (list[str]): 索引中的全部缓存键.

        Returns:
            tuple[list[str], list[str]]: 索引有行但文件缺失的键, 以及文件存在但
            索引无行的键.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start`` (经 ``asyncio.to_thread``)
        """
        known = set(keys)
        present: set[str] = set()
        if self.root.is_dir():
            for shard in self.root.iterdir():
                if shard.is_dir():
                    present.update(
                        entry.name for entry in shard.iterdir() if entry.is_file()
                    )
        stale = [key for key in keys if key not in present]
        return stale, sorted(present - known)

    def _key(self, url: str) -> str:
        """把 URL 映射为定长缓存键.

        Args:
            url (str): 图床缩略图地址.

        Returns:
            str: 十六进制摘要.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._load``
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._save``
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache._apply``
        """
        return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _env_int(name: str, default: int) -> int:
    """读取非负整数环境变量, 未设置或为空时取缺省值.

    Args:
        name (str): 环境变量名.
        default (int): 未设置时的缺省值.

    Returns:
        int: 解析结果, 负数按 0 处理.

    Callers:
        - ``services/online_sources/framework/thumb_cache.py:ThumbCache.__init__``
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return max(0, int(raw))
