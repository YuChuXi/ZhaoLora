"""米画师 API 客户端: 签名请求头装配与 /api/v1 接口访问.

米画师接口无需登录与 cookie, 仅要求 ``M-S``/``M-T``/``Web-Version`` 三个
签名头, 由同包 ``signer`` 模块提供 (时间戳按签名路径严格递增以满足服务端
重放检测)。

WAF 行为 (实测): IP 存在历史劣迹期间, 每个新建 TLS 连接的第一个请求会被
WAF 以 403 ``{"reason":"r_1"}`` 质询拦截, 同一连接的后续请求一律放行, 与
Referer / TLS 指纹 / HTTP 客户端实现无关 (httpx 与 curl_cffi 表现一致)。
因此客户端按事件循环持有, 同一循环内复用连接并仅对 ``r_1`` 质询以新
时间戳重签重试一次 (同连接重试必放行); ``signature invalid`` 类 403
表示签名或重放错误, 不重试直接抛出。批量下载任务经 ``asyncio.run`` 在
工作线程中为每批次创建并关闭独立事件循环, httpx 连接池的 keep-alive
连接绑定创建时所在循环, 跨循环复用同一客户端会以 ``Event loop is
closed`` 报错, 故不得持有跨循环的进程级单例。

Callers:
    - ``services/online_sources/mihuashi/source.py:`` (搜索 / 元数据)
"""

from __future__ import annotations

import asyncio
from typing import Any

from services.online_sources.framework.http import HttpClient
from services.online_sources.mihuashi.signer import ensure_tool, signed_headers

_SITE = "https://www.mihuashi.com"

_clients: dict[asyncio.AbstractEventLoop, HttpClient] = {}


async def _get_client() -> HttpClient:
    """返回当前事件循环的客户端, 同一循环内复用连接.

    客户端按运行循环持有: 同一循环 (同一次批量下载批次或主循环内的搜索)
    复用连接以规避 WAF 新连接首请求质询; 循环关闭后其 keep-alive 连接
    已失效, 对应条目在下次访问时清除, 下一批次拿到全新客户端。

    Returns:
        HttpClient: 当前事件循环唯一的客户端。

    Callers:
        - ``services/online_sources/mihuashi/client.py:api_get``
    """
    loop = asyncio.get_running_loop()
    for closed in [lp for lp in _clients if lp.is_closed()]:
        del _clients[closed]
    client = _clients.get(loop)
    if client is None:
        client = _clients[loop] = HttpClient()
    return client


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
    client = await _get_client()
    headers = signed_headers(path)
    headers["Accept"] = "application/json, text/plain"
    resp = await client.get(_SITE + path, headers=headers, follow_redirects=True)
    if resp.status_code == 403 and "r_1" in resp.text:
        # WAF 新连接首请求质询: 同连接以新时间戳重签重试一次
        headers = signed_headers(path)
        headers["Accept"] = "application/json, text/plain"
        resp = await client.get(_SITE + path, headers=headers, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()
