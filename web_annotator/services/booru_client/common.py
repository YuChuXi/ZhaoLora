"""Booru 公共常量与网络/文件辅助。

引用: ``services.booru_client.search`` / ``services.booru_client.media``。
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

import httpx
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.models import Response as CurlResponse
from PIL import Image

from services.scanner import IMAGE_EXTS

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from DanbooruApi.Gelbooru import Gelbooru  # noqa: E402

SOURCES = frozenset({"gelbooru", "danbooru"})
META_SUBDIR = {
    "gelbooru": ".gelbooru_meta",
    "danbooru": ".danbooru_meta",
}
# 文件名前缀，避免两边数字 id 撞车
STEM_PREFIX = {
    "gelbooru": "g",
    "danbooru": "d",
}
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# TLS/JA3 伪装；普通 httpx 会被 CF 拦
_DANBOORU_IMPERSONATE = "chrome"

# UI 排序 → 站点元标签（Gelbooru sort: / Danbooru order:）
_ORDER_GELBOORU = {
    "score": "sort:score:desc",
    "id": "sort:id:desc",
    "id_asc": "sort:id:asc",
}
_ORDER_DANBOORU = {
    "score": "order:score",
    "id": "order:id_desc",
    "id_asc": "order:id",
}


def build_search_query(
    source: str,
    tags: str,
    *,
    order: str = "",
    extra: str = "",
) -> str:
    """拼标签 + 额外元标签（rating:/width:/score: 等）+ 排序。

    Gelbooru 忽略 ``date:`` 元标签（站内不支持）。

    引用:
        ``routes.booru.booru_search``；``search_posts``。
    """
    parts: list[str] = []
    t = " ".join((tags or "").split())
    if t:
        parts.append(t)
    e_tokens = (extra or "").split()
    if source == "gelbooru":
        e_tokens = [x for x in e_tokens if not x.lower().startswith("date:")]
    e = " ".join(e_tokens)
    if e:
        parts.append(e)
    key = (order or "").strip().lower()
    table = _ORDER_GELBOORU if source == "gelbooru" else _ORDER_DANBOORU
    ord_tag = table.get(key, "")
    if ord_tag:
        parts.append(ord_tag)
    return " ".join(parts).strip()


def _headers(*, referer: str | None = None) -> dict[str, str]:
    """构造图片请求头。引用：fetch_gelbooru_bytes。"""
    h = {
        "user-agent": _DEFAULT_UA,
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        h["referer"] = referer
        if "gelbooru.com" in referer:
            h["cookie"] = "fringeBenefits=yup"
    return h


def _proxy_or_none(proxy: str | None) -> str | None:
    """空代理字符串视为直连。引用：make_gelbooru / _curl_proxies。"""
    p = (proxy or "").strip()
    return p or None


def _curl_proxies(proxy: str | None) -> dict[str, str] | None:
    """curl_cffi 代理字典。引用：_danbooru_get。"""
    p = _proxy_or_none(proxy)
    if not p:
        return None
    return {"http": p, "https": p}


def make_gelbooru(proxy: str | None) -> Gelbooru:
    """构造 Gelbooru 客户端。引用：search / download。"""
    p = _proxy_or_none(proxy)
    return Gelbooru(proxy=p) if p else Gelbooru(proxy=None)  # type: ignore[arg-type]


def _ext_from_url(url: str, fallback: str = ".jpg") -> str:
    """从 URL 猜图像后缀。引用：download_post。"""
    path = url.split("?", 1)[0]
    suf = Path(path).suffix.lower()
    if suf in IMAGE_EXTS:
        return suf
    return fallback


def file_stem(source: str, post_id: str) -> str:
    """规范文件名：g{id} / d{id}，避免源站 id 撞车。"""
    src = source.strip().lower()
    prefix = STEM_PREFIX.get(src, "")
    return f"{prefix}{post_id}"


def _stem_candidates(source: str, post_id: str) -> list[str]:
    """新名优先，兼容旧版纯 id 文件名。"""
    pid = str(post_id).strip()
    primary = file_stem(source, pid)
    out = [primary]
    if pid != primary:
        out.append(pid)
    return out


def _find_existing(out_dir: Path, stem: str) -> Path | None:
    """按 stem 找已有图像。引用：find_downloaded。"""
    for ext in IMAGE_EXTS:
        p = out_dir / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def find_downloaded(out_dir: Path, source: str, post_id: str) -> Path | None:
    """查找已下载帖图。引用：is_downloaded / download_post。"""
    for stem in _stem_candidates(source, post_id):
        hit = _find_existing(out_dir, stem)
        if hit is not None:
            return hit
    return None


def is_downloaded(out_dir: Path, source: str, post_id: str) -> bool:
    """是否已下载。引用：routes.booru.booru_search。"""
    return find_downloaded(out_dir, source, post_id) is not None


def _sniff_image_kind(data: bytes) -> str:
    """根据文件头判断类型：jpeg/png/gif/webp/avif/zip/video/webm/html/empty/unknown。"""
    if not data:
        return "empty"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis", b"mif1", b"msf1", b"heic", b"heif"):
            return "avif"
        return "video"
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    head = data[:80].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<?xml")):
        return "html"
    return "unknown"


_UNSUPPORTED_FILE_EXT = frozenset({"zip", "mp4", "webm", "swf", "mkv", "avi", "ugoira"})


def _save_image_as_png(data: bytes, dest: Path) -> None:
    """任意常见静态图 → PNG（保留透明）。"""
    kind = _sniff_image_kind(data)
    if kind == "empty":
        raise ValueError("下载内容为空")
    if kind == "html":
        raise ValueError("下载到的是网页而非图片（可能被代理/CDN 拦截）")
    if kind in ("zip", "video", "webm"):
        raise ValueError(f"不支持该文件类型（{kind}，视频或动图包）")

    if kind == "png":
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return

    from PIL import UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except UnidentifiedImageError as e:
        raise ValueError(
            f"无法识别的图片数据（头={kind}，大小={len(data)}字节）"
        ) from e

    if img.mode in ("RGBA", "LA"):
        out = img.convert("RGBA")
    elif img.mode == "P":
        out = img.convert("RGBA") if "transparency" in img.info else img.convert("RGB")
    else:
        out = img.convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(dest, format="PNG", optimize=True)


def _convert_path_to_png(src: Path, dest: Path) -> None:
    """临时文件转 PNG。引用：download_post。"""
    data = src.read_bytes()
    if src.is_file():
        src.unlink()
    _save_image_as_png(data, dest)


def _pil_can_open(data: bytes) -> bool:
    """PIL 能否打开字节流。引用：_danbooru_fetch_image_bytes。"""
    from PIL import UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return True
    except UnidentifiedImageError:
        return False


def _write_meta(out_dir: Path, source: str, post_id: str, post: Mapping[str, object]) -> None:
    """写源站帖子 JSON meta。引用：download_post。"""
    meta_dir = out_dir / META_SUBDIR[source]
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{post_id}.json").write_text(
        json.dumps(post, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _tags_to_caption(tags: str) -> str:
    """空格标签转逗号 caption。引用：download_post。"""
    parts = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    return ", ".join(parts)


def danbooru_preview_url(post: Mapping[str, object]) -> str:
    """优先 720x720（webp），避免默认 180x180 预览过小。"""
    ma = post.get("media_asset")
    if isinstance(ma, dict):
        variants = ma.get("variants") or []
        by_type: dict[str, str] = {}
        for v in variants:
            if not isinstance(v, dict):
                continue
            t = v.get("type")
            u = v.get("url")
            if t and u:
                by_type[str(t)] = str(u)
        for key in ("720x720", "360x360", "sample"):
            if by_type.get(key):
                return by_type[key]

    md5 = str(post.get("md5") or "")
    if len(md5) >= 4:
        return f"https://cdn.donmai.us/720x720/{md5[:2]}/{md5[2:4]}/{md5}.webp"

    return str(
        post.get("large_file_url")
        or post.get("preview_file_url")
        or post.get("file_url")
        or ""
    )


def _danbooru_headers(*, accept: str) -> dict[str, str]:
    """Danbooru 请求头。引用：_danbooru_get。"""
    return {
        "accept": accept,
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://danbooru.donmai.us/",
        "origin": "https://danbooru.donmai.us",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def _danbooru_get(
    url: str,
    *,
    proxy: str | None,
    accept: str,
    timeout: float = 30.0,
    params: Mapping[str, str | int] | None = None,
) -> CurlResponse:
    """用 curl_cffi 请求 Danbooru。引用：搜索 / 下载 / 预览。"""
    async with AsyncSession() as client:
        return await client.get(
            url,
            params=dict(params) if params else None,
            headers=_danbooru_headers(accept=accept),
            proxies=_curl_proxies(proxy),
            impersonate=_DANBOORU_IMPERSONATE,
            timeout=timeout,
            allow_redirects=True,
        )

