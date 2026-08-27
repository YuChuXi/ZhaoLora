"""统一网络层: 基于 httpx 的 HTTP 客户端封装.

集中所有来源与路由层对上游的网络请求能力: 代理、UA/防盗链请求头、超时
与流式响应。统一异常为 ``httpx.HTTPError`` (别名 ``NetworkError``),
避免各来源各自混用不同的 HTTP 栈。

代理通过 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量配置,
``httpx`` 原生自动读取, 无需显式传参。

Callers:
    - ``services/online_sources/gelbooru/source.py:`` 模块
    - ``services/online_sources/pixiv/source.py:`` 模块
    - ``routes/online_sources.py:`` 模块
    - ``routes/__init__.py:`` 模块
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from services.core.scanner import IMAGE_EXTS

NetworkError = httpx.HTTPError

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class HttpClient(httpx.AsyncClient):
    """HTTP 客户端, 继承 ``httpx.AsyncClient``, 预置连接池、超时与默认请求头.

    UA 在构造时写入默认请求头, 无需每次调用传入; Referer 可选, 由调用方按需
    追加到请求头。

    每个实例绑定到创建时所在的运行循环, 适合作为异步上下文管理器使用:

        async with HttpClient() as client:
            resp = await client.get(url)

    Callers:
        - ``services/online_sources/gelbooru/source.py:``
        - ``services/online_sources/pixiv/source.py:``
        - ``routes/online_sources.py:``
        - ``services/online_sources/batch.py:`` (批量下载任务自持实例复用连接)
    """

    def __init__(self, *, referer: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("limits", httpx.Limits(max_connections=32, max_keepalive_connections=16))
        kwargs.setdefault("timeout", httpx.Timeout(30.0, connect=10.0))
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("user-agent", DEFAULT_UA)
        if referer:
            headers["referer"] = referer
        kwargs["headers"] = headers
        super().__init__(**kwargs)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _headers_for(self, headers: dict[str, str] | None, referer: str | None) -> dict[str, str]:
        """解析请求头: 显式 ``headers`` 完全替换默认值; ``referer`` 仅追加/覆盖.

        Callers:
            - ``services/online_sources/framework/http.py:HttpClient.get_bytes``
            - ``services/online_sources/framework/http.py:HttpClient.stream_response``
        """
        if headers is not None:
            return headers
        h = dict(self.headers)
        if referer:
            h["referer"] = referer
        return h

    # ------------------------------------------------------------------
    # 业务方法
    # ------------------------------------------------------------------

    async def get_bytes(
        self,
        url: str, *,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        timeout: float = 30.0,
    ) -> tuple[bytes, str]:
        """一次性拉取响应字节与 Content-Type.

        Args:
            url: 目标 URL.
            headers: 自定义请求头, 传入时完全替换默认 UA/Accept/Referer.
            referer: 防盗链 Referer, 与默认请求头合并.
            timeout: 超时秒数.

        Returns:
            tuple[bytes, str]: (字节内容, Content-Type).

        Raises:
            NetworkError: 网络或 HTTP 状态错误.

        Callers:
            - ``services/online_sources/pixiv/source.py:``
            - ``routes/online_sources.py:``
        """
        h = self._headers_for(headers, referer)
        resp = await self.get(url, headers=h, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.content, _ctype(resp)

    async def stream_response(
        self,
        url: str, *,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        timeout: float = 30.0,
    ) -> tuple[str, AsyncIterator[bytes]]:
        """流式获取图片, 返回 (Content-Type, 字节迭代器).

        迭代器内部持有 httpx 连接, 迭代完成后自动关闭。调用方直接传入
        ``StreamingResponse(chunks, media_type=ctype)`` 即可。

        Args:
            url: 目标 URL.
            headers: 自定义请求头, 传入时完全替换默认 UA/Accept/Referer.
            referer: 防盗链 Referer, 与默认请求头合并.
            timeout: 超时秒数.

        Returns:
            tuple[str, AsyncIterator[bytes]]: (Content-Type, 字节迭代器).

        Raises:
            NetworkError: 网络或 HTTP 状态错误.

        Callers:
            - ``routes/online_sources.py:`` (各图片代理端点)
        """
        h = self._headers_for(headers, referer)
        client = HttpClient(timeout=httpx.Timeout(timeout, connect=10.0))
        req = client.build_request("GET", url, headers=h)
        resp = await client.send(req, stream=True)
        resp.raise_for_status()
        ctype = _ctype(resp)

        async def _iter() -> AsyncIterator[bytes]:
            """流式读取字节, 完成后自动关闭客户端与响应."""
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return ctype, _iter()


# ---------------------------------------------------------------------------
# 纯工具函数
# ---------------------------------------------------------------------------


def ext_from_url(url: str, fallback: str = ".jpg") -> str:
    """从 URL 提取图片扩展名, 未知则回退.

    Callers:
        - ``services/online_sources/gelbooru/source.py:`` 模块
    """
    path = url.split("?", 1)[0]
    suf = Path(path).suffix.lower()
    return suf if suf in IMAGE_EXTS else fallback


def _ctype(resp: httpx.Response) -> str:
    return (resp.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
