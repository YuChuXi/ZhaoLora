"""Danbooru 底层网络: JSON API 请求, 网络栈经环境变量切换.

封装 Danbooru 专属网络请求; 代理从 ``HTTP_PROXY`` / ``HTTPS_PROXY``
环境变量读取, 认证 (login / api_key) 由调用方经参数传入.

网络栈由环境变量 ``DANBOORU_CLIENT`` 选择 (忽略大小写, 默认 httpx):

- ``httpx``: 框架统一网络层 ``HttpClient``, 当前 Danbooru 正常响应。
- ``curl_cffi``: Chrome TLS 指纹模拟 (curl_cffi ``AsyncSession``), 作为
  上游变更防火墙策略、封锁 httpx 特征时的备选; 2026-09 实测全部指纹
  均被 403, 故不作为默认。

Callers:
    - ``services/online_sources/danbooru/source.py:`` (搜索 / 下载 / 元数据)
"""

from __future__ import annotations

import os
from typing import Any

from services.online_sources.framework.http import HttpClient

_DANBOORU_BASE = "https://danbooru.donmai.us"
# curl_cffi 模式的 Chrome TLS 指纹
_IMPERSONATE = "chrome"
# (http_proxy, https_proxy) -> curl_cffi 连接池
_cffi_clients: dict[tuple[str, str], Any] = {}


def _mode() -> str:
    """读取当前网络栈模式.

    Returns:
        str: ``"httpx"`` 或 ``"curl_cffi"``.

    Callers:
        - ``services/online_sources/danbooru/client.py:get``
    """
    return os.environ.get("DANBOORU_CLIENT", "httpx").strip().lower() or "httpx"


def _headers(*, accept: str) -> dict[str, str]:
    """Danbooru API 请求头.

    Callers:
        - ``services/online_sources/danbooru/client.py:_get_httpx``
        - ``services/online_sources/danbooru/client.py:_get_cffi``
    """
    return {
        "accept": accept, "accept-language": "en-US,en;q=0.9",
        "referer": f"{_DANBOORU_BASE}/",
        "origin": _DANBOORU_BASE,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def _cffi_session() -> Any:
    """按代理复用的 curl_cffi 连接池, 固定 Chrome TLS 指纹.

    Callers:
        - ``services/online_sources/danbooru/client.py:_get_cffi``
    """
    from curl_cffi.requests import AsyncSession

    http_p = os.environ.get("HTTP_PROXY", "").strip()
    https_p = os.environ.get("HTTPS_PROXY", "").strip()
    key = (http_p, https_p)
    c = _cffi_clients.get(key)
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
        _cffi_clients[key] = c
    return c


async def _get_httpx(url: str, *, accept: str, timeout: float,
                     params: dict[str, str | int] | None) -> Any:
    """经框架统一网络层请求 Danbooru.

    Callers:
        - ``services/online_sources/danbooru/client.py:get``
    """
    async with HttpClient() as client:
        return await client.get(
            url, params=params or None, headers=_headers(accept=accept),
            timeout=timeout, follow_redirects=True,
        )


async def _get_cffi(url: str, *, accept: str, timeout: float,
                    params: dict[str, str | int] | None) -> Any:
    """经 curl_cffi 指纹模拟请求 Danbooru.

    Callers:
        - ``services/online_sources/danbooru/client.py:get``
    """
    return await _cffi_session().get(
        url, params=params or None, headers=_headers(accept=accept),
        timeout=timeout, allow_redirects=True,
    )


async def get(
    url: str, *, accept: str, timeout: float = 30.0,
    params: dict[str, str | int] | None = None,
    login: str | None = None, api_key: str | None = None,
) -> Any:
    """请求 Danbooru API, 按 ``DANBOORU_CLIENT`` 分派网络栈.

    Args:
        url: 目标 URL。
        accept: Accept 请求头。
        timeout: 超时秒数。
        params: 查询参数。
        login / api_key: Danbooru API 认证, 两者需同时提供。

    Returns:
        响应对象 (httpx 或 curl_cffi), 均提供 ``status_code`` / ``json()``。

    Raises:
        NetworkError: httpx 模式网络错误。
        curl_cffi.requests.exceptions.RequestException: curl_cffi 模式网络错误。

    Callers:
        - ``services/online_sources/danbooru/source.py:`` (搜索 / 元数据 / 下载)
        - ``services/online_sources/danbooru/client.py:autocomplete``
    """
    p = dict(params) if params else {}
    if login and api_key:
        p["login"] = login
        p["api_key"] = api_key
    impl = _get_cffi if _mode() == "curl_cffi" else _get_httpx
    return await impl(url, accept=accept, timeout=timeout, params=p)


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
        NetworkError: 网络异常由调用方处理.

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource.autocomplete``
    """
    resp = await get(
        f"{_DANBOORU_BASE}/autocomplete",
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
