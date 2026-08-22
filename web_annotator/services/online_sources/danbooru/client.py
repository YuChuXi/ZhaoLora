"""Danbooru 底层网络: TLS 指纹模拟 + JSON API 请求.

封装 Danbooru 专属网络请求 (curl_cffi 模拟 Chrome TLS 指纹绕过
Cloudflare); 代理从 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量读取,
认证 (login / api_key) 由调用方经参数传入.

Callers:
    - ``services/online_sources/danbooru/source.py:`` (搜索 / 下载 / 元数据)
"""

from __future__ import annotations

import os
from typing import Any

from curl_cffi.requests import AsyncSession

# Danbooru 需模拟 Chrome TLS 指纹绕过 Cloudflare
_IMPERSONATE = "chrome"

# (http_proxy, https_proxy, 指纹) -> 复用连接池
_clients: dict[tuple[str, str, str], AsyncSession] = {}


def _client() -> AsyncSession:
    """按代理复用的 curl_cffi 连接池, 固定 Chrome TLS 指纹.

    代理从 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量读取。

    Callers:
        - ``services/online_sources/danbooru/client.py:get``
    """
    http_p = os.environ.get("HTTP_PROXY", "").strip()
    https_p = os.environ.get("HTTPS_PROXY", "").strip()
    key = (http_p, https_p, _IMPERSONATE)
    c = _clients.get(key)
    if c is None:
        proxies = {}
        if http_p:
            proxies["http"] = http_p
        if https_p:
            proxies["https"] = https_p
        c = AsyncSession(
            impersonate=_IMPERSONATE,  # type: ignore[arg-type]
            proxies=proxies or None,  # type: ignore[arg-type]
        )
        _clients[key] = c
    return c


def _headers(*, accept: str) -> dict[str, str]:
    """Danbooru API 请求头.

    Callers:
        - ``services/online_sources/danbooru/client.py:get``
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
        - ``services/online_sources/danbooru/source.py:`` (搜索 / 元数据 / 下载)
    """
    p = dict(params) if params else {}
    if login and api_key:
        p["login"] = login
        p["api_key"] = api_key
    return await _client().get(
        url, params=p or None, headers=_headers(accept=accept),
        timeout=timeout, allow_redirects=True,
    )


async def autocomplete(query: str, limit: int = 10) -> list[dict]:
    """Danbooru 标签自动补全 (官方 autocomplete 端点).

    ``search[type]=tag`` 时返回 JSON 数组; ``tag_query`` 则返回 HTML 菜单片段。

    Args:
        query: 标签前缀.
        limit: 返回条数上限.

    Returns:
        规范化的建议列表: 每项为 ``{"name", "category", "count"}``。

    Raises:
        RuntimeError: 上游返回非 200.
        httpx/curl_cffi 网络异常由调用方处理.

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource.autocomplete``
    """
    resp = await get(
        "https://danbooru.donmai.us/autocomplete",
        accept="application/json", timeout=15.0,
        params={"search[query]": query, "search[type]": "tag", "version": "3", "limit": limit},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Danbooru autocomplete HTTP {resp.status_code}")
    data = resp.json()
    out: list[dict] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("value")
        if not name:
            continue
        out.append({
            "name": str(name),
            "category": int(item.get("category") or 0),
            "count": int(item.get("post_count") or 0),
        })
    return out
