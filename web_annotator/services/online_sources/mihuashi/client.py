"""米画师 API 客户端: 签名请求头装配与 /api/v1 接口访问.

米画师接口无需登录与 cookie, 仅要求 ``M-S``/``M-T``/``Web-Version`` 三个
签名头, 由同包 ``signer`` 模块提供 (时间戳按签名路径严格递增以满足服务端
重放检测)。

WAF 行为 (实测): IP 存在历史劣迹期间, 每个新建 TLS 连接的第一个请求会被
WAF 以 403 ``{"reason":"r_1"}`` 质询拦截, 同一连接的后续请求一律放行, 与
Referer / TLS 指纹 / HTTP 客户端实现无关 (httpx 与 curl_cffi 表现一致)。
因此仅对 ``r_1`` 质询以新时间戳重签重试一次 (同连接重试必放行);
``signature invalid`` 类 403 表示签名或重放错误, 不重试直接抛出。
连接由统一网络层按事件循环保活复用, 不另建客户端。

Callers:
    - ``services/online_sources/mihuashi/source.py:`` (搜索 / 元数据)
"""

from __future__ import annotations

from typing import Any

from services.core.http import http_session
from services.online_sources.mihuashi.signer import ensure_tool, signed_headers

_SITE = "https://www.mihuashi.com"


async def api_get(path: str) -> dict[str, Any]:
    """GET 米画师 ``/api/v1`` 接口并返回 JSON 响应体.

    Args:
        path (str): 接口相对路径, 可含 query (签名输入自动取不含 query 的纯路径)。

    Returns:
        dict[str, Any]: 响应 JSON。

    Raises:
        httpx.HTTPStatusError: HTTP 状态错误 (含签名失效)。

    Callers:
        - ``services/online_sources/mihuashi/source.py:MihuashiSource.search``
        - ``services/online_sources/mihuashi/source.py:MihuashiSource.fetch_meta``
    """
    await ensure_tool()
    session = http_session()
    headers = signed_headers(path)
    headers["Accept"] = "application/json, text/plain"
    resp = await session.get(_SITE + path, headers=headers)
    if resp.status_code == 403 and "r_1" in resp.text:
        # WAF 新连接首请求质询: 同连接以新时间戳重签重试一次
        headers = signed_headers(path)
        headers["Accept"] = "application/json, text/plain"
        resp = await session.get(_SITE + path, headers=headers)
    resp.raise_for_status()
    return resp.json()
