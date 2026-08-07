"""统一网络层: 基于 httpx 的单 HTTP 客户端封装.

集中所有来源与路由层对上游的网络请求能力: 代理、UA/防盗链请求头、超时
与流式响应。统一异常为 ``httpx.HTTPError`` (别名 ``NetworkError``),
避免各来源各自混用不同的 HTTP 栈。

Callers:
    - ``online_sources.gelbooru.source``
    - ``online_sources.pixiv.source``
    - ``online_sources.bilibili``
    - ``routes.online_sources``
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from services.core.scanner import IMAGE_EXTS

NetworkError = httpx.HTTPError

# 统一 UA: 各来源复用, 不再各自写字符串
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _upstream_proxy() -> str | None:
    """从环境变量读取上游代理, 空串视为无代理.

    Callers:
        - ``framework.http`` 内部
    """
    p = os.environ.get("UPSTREAM_PROXY", "").strip()
    return p or None


# proxy key -> 复用连接池
_clients: dict[str, httpx.AsyncClient] = {}


def make_headers(*, referer: str | None = None) -> dict[str, str]:
    """构造通用请求头, 可选防盗链 Referer.

    Gelbooru 图床需 ``fringeBenefits`` cookie 才返回受限标签内容。

    Callers:
        - ``framework.http.request`` / ``get_bytes`` / ``stream_response``
    """
    h = {
        "user-agent": DEFAULT_UA,
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        h["referer"] = referer
        if "gelbooru.com" in referer:
            h["cookie"] = "fringeBenefits=yup"
    return h


def ext_from_url(url: str, fallback: str = ".jpg") -> str:
    """从 URL 提取图片扩展名, 未知则回退.

    Callers:
        - ``online_sources.gelbooru.source``
    """
    path = url.split("?", 1)[0]
    suf = Path(path).suffix.lower()
    return suf if suf in IMAGE_EXTS else fallback


def _client(proxy: str | None = None) -> httpx.AsyncClient:
    """按代理复用的连接池.

    Callers:
        - ``framework.http.request`` / ``get_bytes``
    """
    key = (proxy or _upstream_proxy() or "")
    c = _clients.get(key)
    if c is not None and not c.is_closed:
        return c
    c = httpx.AsyncClient(
        proxy=proxy or _upstream_proxy(),
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    _clients[key] = c
    return c


def _ctype(resp: httpx.Response) -> str:
    return (resp.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()


async def request(
    url: str, *,
    headers: dict[str, str] | None = None,
    params: dict[str, str | int] | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """发起 GET 请求并返回原始响应 (调用方自行判 HTTP 状态).

    代理默认从 ``UPSTREAM_PROXY`` 环境变量读取, 可通过 ``proxy`` 参数覆盖。

    Args:
        url: 目标 URL。
        headers: 请求头 (可选, 不传则使用默认 UA/Accept)。
        params: 查询参数。
        proxy: 上游代理地址 (可选, 默认 ``UPSTREAM_PROXY`` 环境变量)。
        timeout: 超时秒数。

    Returns:
        httpx.Response: 响应对象。

    Raises:
        NetworkError: 网络错误。

    Callers:
        - ``online_sources.gelbooru.source`` / ``online_sources.pixiv.source`` / ``online_sources.bilibili``
    """
    return await _client(proxy).get(
        url, headers=headers, params=params,
        timeout=timeout, follow_redirects=True,
    )


async def get_bytes(
    url: str, *,
    headers: dict[str, str] | None = None,
    referer: str | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> tuple[bytes, str]:
    """一次性拉取响应字节与 Content-Type.

    Args:
        url: 目标 URL。
        headers: 自定义请求头; 不传则使用 ``make_headers(referer=referer)``。
        referer: 防盗链 Referer。
        proxy: 上游代理地址。
        timeout: 超时秒数。

    Returns:
        tuple[bytes, str]: (字节内容, Content-Type)。

    Raises:
        NetworkError: 网络或 HTTP 状态错误。

    Callers:
        - ``online_sources.pixiv.source`` / ``online_sources.bilibili``
    """
    h = headers or make_headers(referer=referer)
    resp = await request(url, headers=h, proxy=proxy, timeout=timeout)
    resp.raise_for_status()
    return resp.content, _ctype(resp)


async def stream_response(
    url: str, *,
    headers: dict[str, str] | None = None,
    referer: str | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> tuple[str, AsyncIterator[bytes]]:
    """流式获取图片, 返回 (Content-Type, 字节迭代器).

    迭代器内部持有 httpx 连接, 迭代完成后自动关闭。调用方直接传入
    ``StreamingResponse(chunks, media_type=ctype)`` 即可。

    Args:
        url: 目标 URL。
        headers: 自定义请求头; 不传则使用 ``make_headers(referer=referer)``。
        referer: 防盗链 Referer。
        proxy: 上游代理地址 (可选, 默认 ``UPSTREAM_PROXY`` 环境变量)。
        timeout: 超时秒数。

    Returns:
        tuple[str, AsyncIterator[bytes]]: (Content-Type, 字节迭代器)。

    Raises:
        NetworkError: 网络或 HTTP 状态错误。

    Callers:
        - ``routes.online_sources`` (各图片代理端点)
    """
    h = headers or make_headers(referer=referer)
    client = httpx.AsyncClient(
        proxy=proxy or _upstream_proxy(),
        timeout=httpx.Timeout(timeout, connect=10.0),
    )
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