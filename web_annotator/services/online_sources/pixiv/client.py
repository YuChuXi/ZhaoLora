"""pixiv 底层网络: PHPSESSID cookie 读取 + web AJAX 请求 + pximg 图片拉取.

使用 PHPSESSID cookie 认证, 直接调用 ``www.pixiv.net/ajax/`` 接口,
无需 OAuth refresh-token。cookie 从 ``os.environ["PIXIV_COOKIES"]``
读取 (Netscape 格式, 由 ``server.py`` 解析为绝对路径后写入).

Callers:
    - ``pixiv/source.py`` (搜索 / 下载 / 元数据)
    - ``routes/online_sources.py`` (图片代理, ``fetch_pixiv_bytes``)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from services.online_sources.framework import http

# pixiv web AJAX 请求头
_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.pixiv.net/",
    "Accept": "application/json",
}

# pximg 图床防盗链: 需带 pixiv Referer
_IMG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.pixiv.net/",
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
}


def _read_phpsessid() -> str | None:
    """从 Netscape 格式 cookies 文件读出 PHPSESSID 值.

    pixiv web AJAX API 只认 PHPSESSID, 无需加载整个 cookie jar。

    File path is read from ``os.environ["PIXIV_COOKIES"]``.

    Returns:
        str | None: PHPSESSID 值; 文件未配置或不存在时返回 None.
    """
    path = os.environ.get("PIXIV_COOKIES", "").strip()
    if not path:
        return None
    for line in Path(path).read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] == "PHPSESSID":
            return parts[6]
    return None


def request_headers() -> dict[str, str]:
    """构造 pixiv web AJAX 请求头, 含 PHPSESSID cookie.

    Returns:
        dict[str, str]: 请求头字典。

    Callers:
        - `PixivSource._request`
    """
    phpsessid = _read_phpsessid()
    headers = dict(_WEB_HEADERS)
    if phpsessid:
        headers["Cookie"] = f"PHPSESSID={phpsessid}"
    return headers


async def fetch_bytes(url: str) -> tuple[bytes, str]:
    """带 pixiv Referer 拉取 pximg 图片字节与 Content-Type.

    供缩略图/大图代理与下载共用, 规避 pximg 防盗链 (需 ``Referer:
    https://www.pixiv.net/``)。对网络异常指数退避有限重试, 重试耗尽后
    抛出最后一次异常, 不吞没错误。

    Args:
        url: pximg 图床 URL。

    Returns:
        tuple[bytes, str]: ``(字节内容, Content-Type)``。

    Raises:
        http.NetworkError: 网络或 HTTP 状态错误 (重试后仍失败)。

    Callers:
        - ``pixiv/source.py:PixivSource.download``
        - ``routes/online_sources.py`` (图片代理)
    """
    last_exc: http.NetworkError | None = None
    for attempt in range(1, 4):
        try:
            return await http.get_bytes(
                url, headers=_IMG_HEADERS, timeout=120.0,
            )
        except http.NetworkError as exc:
            last_exc = exc
            if attempt < 3:
                await asyncio.sleep(1.5 * (2 ** (attempt - 1)))
    raise last_exc or http.NetworkError(f"图片拉取失败: {url}")