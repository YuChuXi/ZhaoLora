"""Booru 帖子规范化与搜索。

引用: ``routes.booru.booru_search``。
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from services.models import BooruPost
from services.booru_client.common import (
    SOURCES,
    _danbooru_get,
    build_search_query,
    danbooru_preview_url,
    make_gelbooru,
)

def normalize_gelbooru_post(post: Mapping[str, object]) -> BooruPost | None:
    """将 Gelbooru 原始帖子规范化为 ``BooruPost``。

    引用:
        ``search_gelbooru``。
    """
    pid = post.get("id")
    if pid is None:
        return None
    file_url = str(post.get("file_url") or "")
    if not file_url:
        return None
    preview = str(post.get("preview_url") or post.get("sample_url") or file_url)
    sample = str(post.get("sample_url") or preview)
    tags = str(post.get("tags") or "")
    return BooruPost(
        source="gelbooru",
        id=str(pid),
        key=f"gelbooru:{pid}",
        width=int(post.get("width") or 0),
        height=int(post.get("height") or 0),
        score=int(post.get("score") or 0),
        tags=tags,
        preview_url=preview,
        sample_url=sample,
        file_url=file_url,
        rating=str(post.get("rating") or ""),
    )


def normalize_danbooru_post(post: Mapping[str, object]) -> BooruPost | None:
    """将 Danbooru 原始帖子规范化为 ``BooruPost``。

    引用:
        ``search_danbooru``。
    """
    if post.get("is_banned") or post.get("is_deleted"):
        return None
    pid = post.get("id")
    file_url = str(post.get("file_url") or "")
    if pid is None or not file_url:
        return None
    preview = danbooru_preview_url(post)
    sample = str(post.get("large_file_url") or preview)
    tags = str(post.get("tag_string") or "")
    return BooruPost(
        source="danbooru",
        id=str(pid),
        key=f"danbooru:{pid}",
        width=int(post.get("image_width") or 0),
        height=int(post.get("image_height") or 0),
        score=int(post.get("score") or 0),
        tags=tags,
        preview_url=preview,
        sample_url=sample,
        file_url=file_url,
        rating=str(post.get("rating") or ""),
        file_ext=str(post.get("file_ext") or ""),
    )


async def search_gelbooru(
    tags: str,
    *,
    page: int,
    proxy: str | None,
) -> tuple[list[BooruPost], bool]:
    """搜索 Gelbooru。

    引用:
        ``search_posts``。
    """
    api = make_gelbooru(proxy)
    pid = max(0, page - 1)
    enc = quote(tags.strip(), safe="")
    raw_resp = await api.getJson(f"tags={enc}&pid={pid}")
    attrs = raw_resp.get("@attributes") if isinstance(raw_resp, dict) else None
    raw = raw_resp.get("post") if isinstance(raw_resp, dict) else None
    if raw is None:
        posts_raw: list = []
    elif isinstance(raw, dict):
        posts_raw = [raw]
    else:
        posts_raw = list(raw)

    # 按 API 原始条数 / 总数判断，避免 normalize 过滤后误报没有更多
    limit = 100
    offset = pid * limit
    if isinstance(attrs, dict):
        limit = int(attrs.get("limit") or limit)
        offset = int(attrs.get("offset") or offset)
        total = attrs.get("count")
        if total is not None:
            has_more = offset + len(posts_raw) < int(total)
        else:
            has_more = len(posts_raw) >= limit
    else:
        has_more = len(posts_raw) >= limit

    out: list[BooruPost] = []
    for post in posts_raw:
        n = normalize_gelbooru_post(post)
        if n is not None:
            out.append(n)
    return out, has_more


async def search_danbooru(
    tags: str,
    *,
    page: int,
    limit: int,
    proxy: str | None,
) -> tuple[list[BooruPost], bool]:
    """搜索 Danbooru。

    引用:
        ``search_posts``。
    """
    limit_used = max(1, min(limit, 100))
    resp = await _danbooru_get(
        "https://danbooru.donmai.us/posts.json",
        proxy=proxy,
        accept="application/json, text/plain, */*",
        timeout=30.0,
        params={
            "tags": tags.strip(),
            "page": max(1, page),
            "limit": limit_used,
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Danbooru HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not isinstance(data, list):
        return [], False
    # 用 API 原始条数判断；normalize 会丢掉无 file_url 等帖
    has_more = len(data) >= limit_used
    out: list[BooruPost] = []
    for post in data:
        if not isinstance(post, dict):
            continue
        n = normalize_danbooru_post(post)
        if n is not None:
            out.append(n)
    return out, has_more


async def search_posts(
    source: str,
    tags: str,
    *,
    page: int = 1,
    limit: int = 40,
    order: str = "",
    extra: str = "",
    proxy: str | None = None,
) -> tuple[list[BooruPost], bool]:
    """按源站搜索帖子。

    引用:
        ``routes.booru.booru_search``。
    """
    source = source.strip().lower()
    if source not in SOURCES:
        raise ValueError(f"不支持的源: {source}")
    query = build_search_query(source, tags, order=order, extra=extra)
    if source == "gelbooru":
        return await search_gelbooru(query, page=page, proxy=proxy)
    return await search_danbooru(query, page=page, limit=limit, proxy=proxy)

