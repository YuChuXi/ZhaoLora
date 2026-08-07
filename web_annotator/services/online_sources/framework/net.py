"""Booru 网络请求与来源特定辅助. 

Callers:
    - ``sources``
    - ``media``
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.models import Response as CurlResponse

from services.core.scanner import IMAGE_EXTS
from services.models.booru import BooruPost

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from DanbooruApi.Gelbooru import Gelbooru  # noqa: E402

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DANBOORU_IMPERSONATE = "chrome"
UNSUPPORTED_FILE_EXT = frozenset({"zip", "mp4", "webm", "swf", "mkv", "avi", "ugoira"})


def proxy_or_none(proxy: str | None) -> str | None:
    p = (proxy or "").strip()
    return p or None


def _curl_proxies(proxy: str | None) -> dict[str, str] | None:
    p = proxy_or_none(proxy)
    return {"http": p, "https": p} if p else None


def make_headers(*, referer: str | None = None) -> dict[str, str]:
    h = {
        "user-agent": _DEFAULT_UA,
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        h["referer"] = referer
        if "gelbooru.com" in referer:
            h["cookie"] = "fringeBenefits=yup"
    return h


def ext_from_url(url: str, fallback: str = ".jpg") -> str:
    path = url.split("?", 1)[0]
    suf = Path(path).suffix.lower()
    return suf if suf in IMAGE_EXTS else fallback


def _danbooru_headers(*, accept: str) -> dict[str, str]:
    return {
        "accept": accept, "accept-language": "en-US,en;q=0.9",
        "referer": "https://danbooru.donmai.us/",
        "origin": "https://danbooru.donmai.us",
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def danbooru_get(
    url: str, *, proxy: str | None, accept: str, timeout: float = 30.0,
    params: Mapping[str, str | int] | None = None,
    login: str | None = None, api_key: str | None = None,
) -> CurlResponse:
    """请求 Danbooru API, 模拟 Chrome TLS 指纹绕过 Cloudflare. 

    Args:
        url: 目标 URL. 
        proxy: 代理地址, 空串视为无代理. 
        accept: Accept 请求头. 
        timeout: 超时秒数. 
        params: 查询参数. 
        login / api_key: Danbooru API 认证, 两者需同时提供. 

    Returns:
        curl_cffi 响应对象. 

    Callers:
        - `danbooru.py:DanbooruSource.search`
        - `danbooru.py:DanbooruSource.fetch_meta`
        - `danbooru.py:DanbooruSource.download`
        - `media.py:_danbooru_fetch_image_bytes`
    """
    p = dict(params) if params else {}
    if login and api_key:
        p["login"] = login; p["api_key"] = api_key
    async with AsyncSession(impersonate=_DANBOORU_IMPERSONATE) as client:
        return await client.get(
            url, params=p if p else None, headers=_danbooru_headers(accept=accept),
            proxies=_curl_proxies(proxy),  # type: ignore[arg-type]
            timeout=timeout, allow_redirects=True,
        )


def make_gelbooru(proxy: str | None) -> Gelbooru:
    """构造 Gelbooru 客户端. 

    Callers:
        - `gelbooru.py:GelbooruSource`
        - `media.py:fetch_gelbooru_bytes`
    """
    p = proxy_or_none(proxy)
    return Gelbooru(proxy=p) if p else Gelbooru(proxy=None)  # type: ignore[arg-type]


def danbooru_preview_url(post: BooruPost) -> str:
    """Danbooru 帖的预览 URL, 优先 720x720 webp, 回退 file_url. 

    Callers:
        - `danbooru.py:DanbooruSource.preview_url`
    """
    if post.media_asset:
        variants = post.media_asset.get("variants") or []
        for v in variants:
            if not isinstance(v, dict):
                continue
            t = v.get("type")
            if t == "720x720":
                return str(v.get("url", ""))
        for v in variants:
            if isinstance(v, dict) and v.get("type") in ("360x360", "sample"):
                return str(v.get("url", ""))
    if len(post.md5) >= 4:
        return f"https://cdn.donmai.us/720x720/{post.md5[:2]}/{post.md5[2:4]}/{post.md5}.webp"
    return post.large_file_url or post.preview_url or post.file_url
