"""按会话管理游标式来源的续拉缓存.

游标式来源 (Bilibili/X) 无法按页码随机访问, 改用搜索会话: 会话持有续拉
游标与已拉缓存, 前端持 token 按批续拉; 发起新搜索或会话超过一天无翻页
时被销毁重建, 避免缓存无限常驻。

Callers:
    - `bilibili/source.py:BilibiliSource`
    - `framework/gallerydl.py:GalleryDLSource`
"""

from __future__ import annotations

import asyncio
import time
import uuid

# 会话空闲存活时长: 一天无翻页则销毁
_SESSION_TTL = 24 * 3600.0


class Session:
    """一次搜索会话: 由 token 标识, 持有来源续拉状态与活跃时间."""

    def __init__(self, source: str, query_key: object) -> None:
        """初始化会话.

        Args:
            source: 来源名, 用于校验 token 归属.
            query_key: 归一化查询键, 用于识别是否为新搜索.
        """
        self.token = uuid.uuid4().hex
        self.source = source
        self.query_key = query_key
        self.created = time.time()
        self.last_active = self.created
        self.lock = asyncio.Lock()
        # 由来源填充的续拉状态 (bilibili: 动态游标; gallerydl: 缓存+游标)
        self.state: dict = {}


class SessionStore:
    """进程内会话存储.

    会话过期/失效采用惰性回收: 访问时才校验 token、来源、查询键与空闲时长,
    不匹配即用新会话替换; 新建时顺带清理过期会话, 防止池无限增长。
    """

    def __init__(self, ttl: float = _SESSION_TTL) -> None:
        """初始化存储.

        Args:
            ttl: 会话空闲存活时长 (秒).
        """
        self._ttl = ttl
        self._sessions: dict[str, Session] = {}
        self._guard = asyncio.Lock()

    async def acquire(self, token: str, source: str, query_key: object) -> Session:
        """取(或建)会话.

        Args:
            token: 前端回传的会话 token; 空表示新搜索.
            source: 来源名.
            query_key: 归一化查询键.

        Returns:
            Session: 有效会话; token 缺失/未知/来源或查询键不符/空闲超时
            时, 返回新建会话并丢弃旧会话.

        Callers:
            - `bilibili/source.py:BilibiliSource.search_session`
            - `framework/gallerydl.py:GalleryDLSource.search_session`
        """
        async with self._guard:
            now = time.time()
            sess = self._sessions.get(token) if token else None
            if (
                sess is None
                or sess.source != source
                or sess.query_key != query_key
                or now - sess.last_active > self._ttl
            ):
                sess = Session(source=source, query_key=query_key)
                self._sessions[sess.token] = sess
                stale = [
                    k
                    for k, s in self._sessions.items()
                    if now - s.last_active > self._ttl
                ]
                for k in stale:
                    self._sessions.pop(k, None)
            else:
                sess.last_active = now
            return sess