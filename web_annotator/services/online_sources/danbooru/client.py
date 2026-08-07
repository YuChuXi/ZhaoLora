"""Danbooru 底层网络: TLS 指纹模拟 + JSON API 请求.

封装 Danbooru 专属网络请求 (curl_cffi 模拟 Chrome TLS 指纹绕过
Cloudflare); 代理从 ``os.environ["UPSTREAM_PROXY"]`` 读取, 认证
(login / api_key) 由调用方经参数传入.

Callers:
    - ``danbooru/source.py`` (搜索 / 下载 / 元数据)
"""

from __future__ import annotations

import os
from typing import Any

from curl_cffi.requests import AsyncSession

# Danbooru 需模拟 Chrome TLS 指纹绕过 Cloudflare
_IMPERSONATE = "chrome"

# (代理, 指纹) -> 复用连接池
_clients: dict[tuple[str, str], AsyncSession] = {}


def _client() -> AsyncSession:
    """按代理复用的 curl_cffi 连接池, 固定 Chrome TLS 指纹.

    代理从 ``os.environ["UPSTREAM_PROXY"]`` 读取。

    Callers:
        - ``danbooru/client.py:get``
    """
    p = os.environ.get("UPSTREAM_PROXY", "").strip() or None
    key = (p or "", _IMPERSONATE)
    c = _clients.get(key)
    if c is None:
        c = AsyncSession(
            impersonate=_IMPERSONATE,  # type: ignore[arg-type]
            proxies={"http": p, "https": p} if p else None,  # type: ignore[arg-type]
        )
        _clients[key] = c
    return c


def _headers(*, accept: str) -> dict[str, str]:
    """Danbooru API 请求头.

    Callers:
        - ``danbooru/client.py:get``
    """
    return {
        "accept": accept, "accept-language": "en-US,en;q=0.9",
        "referer": "https://danbooru.donmai.us/",
        "origin": "https://danbooru.donmai.us",
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def get(
    url: str, *, accept: str, timeout: float = 30.0,
    params: dict[str, str | int] | None = None,
    login: str | None = None, api_key: str | None = None,
) -> Any:
    """请求 Danbooru API, 模拟 Chrome TLS 指纹绕过 Cloudflare.

    Args:
        url: 目标 URL。
        accept: Accept 请求头。
        timeout: 超时秒数。
        params: 查询参数。
        login / api_key: Danbooru API 认证, 两者需同时提供。

    Returns:
        curl_cffi 响应对象。

    Callers:
        - ``danbooru/source.py`` (搜索 / 元数据 / 下载)
    """
    p = dict(params) if params else {}
    if login and api_key:
        p["login"] = login
        p["api_key"] = api_key
    return await _client().get(
        url, params=p or None, headers=_headers(accept=accept),
        timeout=timeout, allow_redirects=True,
    )