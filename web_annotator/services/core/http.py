"""统一网络层: 事件循环级 HTTP 会话与连接池复用.

全部出站 HTTP 请求经 ``http_session()`` 取当前事件循环的会话, 由会话统一
持有连接池. 连接在请求间保活复用、HTTP/2 连接多路复用, 并发连接数由池上限
约束, 不会因请求数量增长而线性占用文件描述符.

会话归属其事件循环: 主循环的会话在 ``server.py:lifespan`` 关闭, 工作线程经
``run_isolated`` 运行的临时循环在其协程结束后关闭.

Callers:
    - ``services/online_sources/framework/base.py:`` (图片代理)
    - ``services/online_sources/`` 各来源客户端
    - ``services/analysis/caption.py:`` (描述服务)
    - ``services/tagging/stats.py:`` (标签翻译)
    - ``server.py:lifespan``
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator, Coroutine
from typing import Any, TypeVar

import httpx

NetworkError = httpx.HTTPError

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# 出站连接上限: 并发请求超出此数时在池内排队, 而不是各自新建 TCP 连接.
_MAX_CONNECTIONS = 64
# 保活连接数上限与空闲回收时间; 复用同一连接的请求免去 TCP 与 TLS 握手.
_MAX_KEEPALIVE = 32
_KEEPALIVE_EXPIRY = 30.0
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# 查询参数: 映射形式 (单值) 或键值对序列 (同名多值, 如翻译接口的多个 q).
QueryParams = dict[str, Any] | list[tuple[str, str]]


class HttpSession:
    """事件循环级 HTTP 会话: 持有按协议版本分离的复用客户端.

    httpx 在构造客户端时固定 ALPN, 同一实例无法同时服务仅支持 HTTP/1.1 的
    上游与要求 HTTP/2 的上游, 故按协议版本各持一个客户端. Referer / Cookie /
    Accept / 超时等差异一律作为请求参数传入, 不派生新客户端.

    Callers:
        - ``services/core/http.py:http_session``
    """

    def __init__(self) -> None:
        self._clients: dict[bool, httpx.AsyncClient] = {}

    def client(self, *, http2: bool = False) -> httpx.AsyncClient:
        """取本会话的客户端, 首次访问时创建并纳入会话管理.

        Args:
            http2 (bool): 是否取 HTTP/2 客户端.

        Returns:
            httpx.AsyncClient: 会话持有的客户端; 生命周期归会话, 调用方不得关闭.

        Callers:
            - ``services/core/http.py:HttpSession.get``
            - ``services/core/http.py:HttpSession.stream``
            - ``services/analysis/caption.py:run_caption`` (流式 chat 请求)
        """
        client = self._clients.get(http2)
        if client is None:
            client = self._clients[http2] = httpx.AsyncClient(
                http2=http2,
                limits=httpx.Limits(
                    max_connections=_MAX_CONNECTIONS,
                    max_keepalive_connections=_MAX_KEEPALIVE,
                    keepalive_expiry=_KEEPALIVE_EXPIRY,
                ),
                timeout=_DEFAULT_TIMEOUT,
                headers={"user-agent": DEFAULT_UA},
                follow_redirects=True,
            )
        return client

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: QueryParams | None = None,
        timeout: httpx.Timeout | float | None = None,
        http2: bool = False,
    ) -> httpx.Response:
        """发起 GET 请求并返回响应对象, 状态码由调用方判定.

        Args:
            url (str): 目标地址.
            headers (dict[str, str] | None): 请求头, 与客户端默认头合并.
            params (QueryParams | None): 查询参数, 支持同名多值.
            timeout (httpx.Timeout | float | None): 本次请求超时; 省略用会话默认值.
            http2 (bool): 是否经 HTTP/2 客户端发送.

        Returns:
            httpx.Response: 已读取响应体的响应对象.

        Raises:
            NetworkError: 连接、协议或读取超时错误.

        Callers:
            - ``services/online_sources/danbooru/client.py:get``
            - ``services/online_sources/e621/client.py:E621Client._api``
            - ``services/online_sources/gelbooru/client.py:GelbooruClient._api``
            - ``services/online_sources/pixiv/source.py:PixivSource._request``
            - ``services/online_sources/bilibili/client.py:`` 各接口
            - ``services/online_sources/mihuashi/client.py:api_get``
            - ``services/online_sources/mihuashi/signer.py:_discover``
            - ``services/online_sources/framework/media.py:download_raw_file``
            - ``services/tagging/stats.py:translate_missing_tags``
        """
        return await self.client(http2=http2).get(
            url, headers=headers, params=params, timeout=timeout,
        )

    async def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
        http2: bool = False,
    ) -> tuple[bytes, str]:
        """一次性拉取响应字节与 Content-Type, 非 2xx 状态即抛错.

        Args:
            url (str): 目标地址.
            headers (dict[str, str] | None): 请求头, 与客户端默认头合并.
            timeout (httpx.Timeout | float | None): 本次请求超时; 省略用会话默认值.
            http2 (bool): 是否经 HTTP/2 客户端发送.

        Returns:
            tuple[bytes, str]: (字节内容, Content-Type).

        Raises:
            NetworkError: 连接错误或 HTTP 状态错误.

        Callers:
            - ``services/online_sources/gelbooru/client.py:GelbooruClient.get_image``
            - ``services/online_sources/pixiv/client.py:fetch_bytes``
            - ``services/online_sources/bilibili/client.py:fetch_bili_bytes``
            - ``services/online_sources/mihuashi/source.py:MihuashiSource.download``
            - ``services/online_sources/framework/gallerydl.py:GalleryDLSource.download``
        """
        resp = await self.get(url, headers=headers, timeout=timeout, http2=http2)
        resp.raise_for_status()
        return resp.content, _ctype(resp)

    async def stream(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
        http2: bool = False,
    ) -> tuple[str, AsyncIterator[bytes]]:
        """发起流式 GET, 返回 Content-Type 与响应体迭代器.

        迭代器耗尽或被关闭时释放响应, 连接随之归还连接池.

        Args:
            url (str): 目标地址.
            headers (dict[str, str] | None): 请求头, 与客户端默认头合并.
            timeout (httpx.Timeout | float | None): 本次请求超时; 省略用会话默认值.
            http2 (bool): 是否经 HTTP/2 客户端发送.

        Returns:
            tuple[str, AsyncIterator[bytes]]: (Content-Type, 字节迭代器).

        Raises:
            NetworkError: 连接错误或 HTTP 状态错误.

        Callers:
            - ``services/online_sources/framework/base.py:SourceClient.proxy_image``
        """
        client = self.client(http2=http2)
        request = client.build_request("GET", url, headers=headers, timeout=timeout)
        response = await client.send(request, stream=True)
        response.raise_for_status()
        return _ctype(response), _body(response)

    async def aclose(self) -> None:
        """关闭会话持有的全部客户端, 释放连接池.

        Returns:
            None: 无返回值.

        Callers:
            - ``services/core/http.py:close_http_session``
        """
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()


_SESSIONS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, HttpSession] = (
    weakref.WeakKeyDictionary()
)

_T = TypeVar("_T")


def http_session() -> HttpSession:
    """取当前事件循环的 HTTP 会话, 不存在时创建.

    Returns:
        HttpSession: 绑定当前运行循环的会话.

    Callers:
        - ``services/online_sources/framework/base.py:SourceClient.proxy_image``
        - ``services/online_sources/`` 各来源客户端
        - ``services/analysis/caption.py:run_caption``
        - ``services/tagging/stats.py:translate_missing_tags``
    """
    loop = asyncio.get_running_loop()
    session = _SESSIONS.get(loop)
    if session is None:
        session = _SESSIONS[loop] = HttpSession()
    return session


async def close_http_session() -> None:
    """关闭当前事件循环的会话并释放连接池.

    Returns:
        None: 无返回值.

    Callers:
        - ``server.py:lifespan``
        - ``services/core/http.py:run_isolated``
    """
    session = _SESSIONS.pop(asyncio.get_running_loop(), None)
    if session is not None:
        await session.aclose()


async def run_isolated(coro: Coroutine[Any, Any, _T]) -> _T:
    """在临时事件循环中执行协程, 结束前关闭该循环的 HTTP 会话.

    工作线程经 ``asyncio.run`` 运行异步任务时, 事件循环随任务结束而关闭;
    循环内的连接池必须在此之前显式释放, 否则保活连接随循环一起丢弃.

    Args:
        coro (Coroutine[Any, Any, _T]): 待执行的协程.

    Returns:
        _T: 协程返回值.

    Callers:
        - ``services/online_sources/batch.py:run_batch``
        - ``routes/online_sources.py:_submit_video_download``
        - ``routes/online_sources.py:refresh_metadata``
    """
    try:
        return await coro
    finally:
        await close_http_session()


async def _body(response: httpx.Response) -> AsyncIterator[bytes]:
    """逐块产出响应体, 结束后释放响应以归还连接.

    Args:
        response (httpx.Response): 流式响应.

    Yields:
        bytes: 响应体分块.

    Callers:
        - ``services/core/http.py:HttpSession.stream``
    """
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()


def _ctype(resp: httpx.Response) -> str:
    """提取响应的媒体类型主值, 去除参数与空白.

    Args:
        resp (httpx.Response): 响应对象.

    Returns:
        str: Content-Type 主值 (如 ``image/jpeg``); 缺失时为 ``application/octet-stream``.

    Callers:
        - ``services/core/http.py:HttpSession.get_bytes``
        - ``services/core/http.py:HttpSession.stream``
    """
    return (resp.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
