"""统一数据库层: 异步引擎、会话工厂与建表.

各业务域只声明自己的 ``DeclarativeBase`` 与模型, 通过 ``Database.open`` 取得
绑定单库的引擎与会话工厂; 引擎归属装配方 (``server.py:lifespan``), 关闭时
调用 ``Database.close`` 释放连接池与文件句柄. 驱动为 ``aiosqlite``, URL 形如
``sqlite+aiosqlite:///绝对路径``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# SQLite 写锁等待毫秒数: 并发写超出此时间即报 database is locked.
_BUSY_TIMEOUT_MS = 30_000
# 每个新连接都要设置的运行参数: 预写日志使读写互不阻塞, NORMAL 同步在 WAL 下
# 只在检查点时刷盘, 单条提交不必每次等待磁盘.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}",
)


def _apply_pragmas(connection, _record) -> None:
    """连接建立时设置 SQLite 运行参数.

    Args:
        connection (sqlite3.Connection): 由驱动新建的同步连接.
        _record (object): SQLAlchemy 连接记录, 未使用.

    Returns:
        None: 无返回值.

    Callers:
        - ``services/core/db.py:Database.open`` (注册为 ``connect`` 事件监听)
    """
    cursor = connection.cursor()
    try:
        for statement in _PRAGMAS:
            cursor.execute(statement)
    finally:
        cursor.close()


@dataclass(frozen=True)
class Database:
    """单个数据库的异步引擎与会话工厂.

    Attributes:
        engine (AsyncEngine): 异步引擎, 生命周期归持有方.
        sessions (async_sessionmaker[AsyncSession]): 会话工厂, 每次操作取独立会话.
    """

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    @classmethod
    def open(cls, path: Path) -> Database:
        """建立父目录并创建绑定该文件的引擎与会话工厂.

        Args:
            path (Path): 数据库文件路径, 通常位于任务根下的 ``.cache/``.

        Returns:
            Database: 已就绪的数据库句柄; 建表由 ``create_all`` 完成.

        Callers:
            - ``server.py:lifespan``
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            connect_args={"timeout": _BUSY_TIMEOUT_MS / 1000},
        )
        event.listen(engine.sync_engine, "connect", _apply_pragmas)
        return cls(
            engine=engine,
            sessions=async_sessionmaker(engine, expire_on_commit=False),
        )

    async def create_all(self, base: type[DeclarativeBase]) -> None:
        """为给定声明基类创建尚未存在的表.

        Args:
            base (type[DeclarativeBase]): 该库对应业务域的声明基类.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.start``
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)

    async def close(self) -> None:
        """释放引擎与连接池.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/online_sources/framework/thumb_cache.py:ThumbCache.close``
        """
        await self.engine.dispose()
