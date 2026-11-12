"""Danbooru 网络层: JSON API 与图床请求.

Danbooru 全站 (含图床 ``cdn.donmai.us``) 由 Cloudflare 边缘节点提供, 该节点
对 HTTP/1.1 的浏览器类请求返回 403 质询页 (``text/html``), 仅 HTTP/2 放行,
故此处请求一律经 ``HttpClient`` 以 HTTP/2 发送。代理从 ``HTTP_PROXY`` /
``HTTPS_PROXY`` 环境变量读取, 认证 (login / api_key) 由调用方经参数传入.

Callers:
    - ``services/online_sources/danbooru/source.py:`` (搜索 / 下载 / 元数据)
"""

from __future__ import annotations

from typing import Any

from services.online_sources.framework.http import HttpClient

_DANBOORU_BASE = "https://danbooru.donmai.us"


def _headers(*, accept: str) -> dict[str, str]:
    """Danbooru 请求头.

    Callers:
        - ``services/online_sources/danbooru/client.py:get``
    """
    return {
        "accept": accept, "accept-language": "en-US,en;q=0.9",
        "referer": f"{_DANBOORU_BASE}/",
        "origin": _DANBOORU_BASE,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def get(
    url: str, *, accept: str, timeout: float = 30.0,
    params: dict[str, str | int] | None = None,
    login: str | None = None, api_key: str | None = None,
) -> Any:
    """请求 Danbooru API 或图床, 固定 HTTP/2.

    Args:
        url (str): 目标 URL。
        accept (str): Accept 请求头。
        timeout (float): 超时秒数。
        params (dict[str, str | int] | None): 查询参数。
        login (str | None) / api_key (str | None): Danbooru API 认证, 两者需同时提供。

    Returns:
        Any: ``httpx.Response``, 提供 ``status_code`` / ``headers`` / ``json()`` / ``content``。

    Raises:
        NetworkError: 网络错误, 即 ``httpx.HTTPError``。

    Callers:
        - ``services/online_sources/danbooru/source.py:`` (搜索 / 元数据 / 下载)
        - ``services/online_sources/danbooru/client.py:autocomplete``
    """
    p = dict(params) if params else {}
    if login and api_key:
        p["login"] = login
        p["api_key"] = api_key
    async with HttpClient(http2=True) as client:
        return await client.get(
            url, params=p or None, headers=_headers(accept=accept),
            timeout=timeout, follow_redirects=True,
        )


def http_error(resp: Any, what: str) -> RuntimeError:
    """构造带上游说明的 HTTP 异常, 供各调用点直接抛出.

    上游 JSON 错误体 (Danbooru 的 ``{"success": false, "message": ...}``) 取
    ``message`` 作为详情; 非 JSON 响应 (如 Cloudflare 质询页) 则标注内容类型,
    便于区分认证 / 限流 / 边缘拦截。

    Args:
        resp (Any): ``httpx.Response``。
        what (str): 请求用途, 如 ``"搜索"`` / ``"元数据"`` / ``"标签补全"``。

    Returns:
        RuntimeError: 含状态码与上游详情的异常。

    Callers:
        - ``services/online_sources/danbooru/client.py:autocomplete``
        - ``services/online_sources/danbooru/source.py:DanbooruSource.fetch_meta``
        - ``services/online_sources/danbooru/source.py:DanbooruSource.search``
    """
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    if ctype == "application/json":
        payload = resp.json()
        if isinstance(payload, dict):
            message = str(payload.get("message") or "").strip()
            if message:
                return RuntimeError(f"Danbooru {what} HTTP {resp.status_code}: {message}")
    return RuntimeError(
        f"Danbooru {what} HTTP {resp.status_code} (Content-Type: {ctype or '未知'})"
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
        raise http_error(resp, "标签补全")
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
