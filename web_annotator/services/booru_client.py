"""Danbooru / Gelbooru 搜索与下载（筛选页挂载）。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from curl_cffi.requests import AsyncSession
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parents[2]
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
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
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
    """拼标签 + 额外元标签（rating:/width:/score: 等）+ 排序。"""
    parts: list[str] = []
    t = " ".join((tags or "").split())
    if t:
        parts.append(t)
    e = " ".join((extra or "").split())
    if e:
        parts.append(e)
    key = (order or "").strip().lower()
    table = _ORDER_GELBOORU if source == "gelbooru" else _ORDER_DANBOORU
    ord_tag = table.get(key, "")
    if ord_tag:
        parts.append(ord_tag)
    return " ".join(parts).strip()


def _headers(*, referer: str | None = None) -> dict[str, str]:
    h = {
        "user-agent": _DEFAULT_UA,
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        h["referer"] = referer
    return h


def _proxy_or_none(proxy: str | None) -> str | None:
    p = (proxy or "").strip()
    return p or None


def _curl_proxies(proxy: str | None) -> dict[str, str] | None:
    p = _proxy_or_none(proxy)
    if not p:
        return None
    return {"http": p, "https": p}


def make_gelbooru(proxy: str | None) -> Gelbooru:
    p = _proxy_or_none(proxy)
    return Gelbooru(proxy=p) if p else Gelbooru(proxy=None)  # type: ignore[arg-type]


def _ext_from_url(url: str, fallback: str = ".jpg") -> str:
    path = url.split("?", 1)[0]
    suf = Path(path).suffix.lower()
    if suf in _IMAGE_EXTS:
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
    for ext in _IMAGE_EXTS:
        p = out_dir / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def find_downloaded(out_dir: Path, source: str, post_id: str) -> Path | None:
    for stem in _stem_candidates(source, post_id):
        hit = _find_existing(out_dir, stem)
        if hit is not None:
            return hit
    return None


def is_downloaded(out_dir: Path, source: str, post_id: str) -> bool:
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
    data = src.read_bytes()
    if src.is_file():
        src.unlink()
    _save_image_as_png(data, dest)


def _pil_can_open(data: bytes) -> bool:
    from PIL import UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return True
    except UnidentifiedImageError:
        return False


async def _danbooru_fetch_image_bytes(
    post: dict[str, Any],
    *,
    proxy: str | None,
) -> bytes:
    """优先原图；失败或不支持时回退 large_file_url。"""
    urls: list[str] = []
    for key in ("file_url", "large_file_url"):
        u = str(post.get(key) or "")
        if u and u not in urls:
            urls.append(u)
    if not urls:
        raise ValueError("该帖无 file_url（可能需登录或已删除）")

    last_err = "无法下载图片"
    for url in urls:
        resp = await _danbooru_get(
            url,
            proxy=proxy,
            accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            timeout=60.0,
        )
        if resp.status_code != 200:
            last_err = f"下载 HTTP {resp.status_code}"
            continue
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/html" in ctype or "application/json" in ctype:
            last_err = f"下载到的不是图片（Content-Type: {ctype}）"
            continue
        data = resp.content
        kind = _sniff_image_kind(data)
        if kind in ("empty", "html", "zip", "video", "webm"):
            last_err = f"不支持该文件类型（{kind}）"
            continue
        if kind == "unknown" and not _pil_can_open(data):
            last_err = f"无法识别的图片数据（大小={len(data)}字节）"
            continue
        return data
    raise ValueError(last_err)


def remove_download(
    source: str,
    post_id: str,
    out_dir: Path,
) -> dict[str, Any]:
    """取消选中时删除已下载的图、旁路 txt 与 meta。"""
    source = source.strip().lower()
    if source not in SOURCES:
        raise ValueError(f"不支持的源: {source}")
    pid = str(post_id).strip()
    # 若误传 g123 / d123，剥掉前缀
    prefix = STEM_PREFIX.get(source, "")
    if prefix and pid.startswith(prefix) and pid[len(prefix) :].isdigit():
        pid = pid[len(prefix) :]
    if not pid or "/" in pid or "\\" in pid or ".." in pid:
        raise ValueError("非法 id")

    removed: list[str] = []
    for stem in _stem_candidates(source, pid):
        img = _find_existing(out_dir, stem)
        if img is not None:
            img.unlink()
            removed.append(img.name)
        tags_path = out_dir / f"{stem}.txt"
        if tags_path.is_file():
            tags_path.unlink()
            removed.append(tags_path.name)

    meta_path = out_dir / META_SUBDIR[source] / f"{pid}.json"
    if meta_path.is_file():
        meta_path.unlink()
        removed.append(str(Path(META_SUBDIR[source]) / f"{pid}.json"))

    return {
        "id": pid,
        "source": source,
        "removed": removed,
        "count": len(removed),
    }


def _write_meta(out_dir: Path, source: str, post_id: str, post: dict[str, Any]) -> None:
    meta_dir = out_dir / META_SUBDIR[source]
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{post_id}.json").write_text(
        json.dumps(post, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _tags_to_caption(tags: str) -> str:
    parts = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    return ", ".join(parts)


def danbooru_preview_url(post: dict[str, Any]) -> str:
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
    params: dict[str, Any] | None = None,
):
    async with AsyncSession() as client:
        return await client.get(
            url,
            params=params,
            headers=_danbooru_headers(accept=accept),
            proxies=_curl_proxies(proxy),
            impersonate=_DANBOORU_IMPERSONATE,
            timeout=timeout,
            allow_redirects=True,
        )


def normalize_gelbooru_post(post: dict[str, Any]) -> dict[str, Any] | None:
    pid = post.get("id")
    if pid is None:
        return None
    file_url = str(post.get("file_url") or "")
    if not file_url:
        return None
    preview = str(post.get("preview_url") or post.get("sample_url") or file_url)
    sample = str(post.get("sample_url") or preview)
    tags = str(post.get("tags") or "")
    return {
        "source": "gelbooru",
        "id": str(pid),
        "key": f"gelbooru:{pid}",
        "width": int(post.get("width") or 0),
        "height": int(post.get("height") or 0),
        "score": int(post.get("score") or 0),
        "tags": tags,
        "preview_url": preview,
        "sample_url": sample,
        "file_url": file_url,
        "rating": str(post.get("rating") or ""),
    }


def normalize_danbooru_post(post: dict[str, Any]) -> dict[str, Any] | None:
    if post.get("is_banned") or post.get("is_deleted"):
        return None
    pid = post.get("id")
    file_url = str(post.get("file_url") or "")
    if pid is None or not file_url:
        return None
    preview = danbooru_preview_url(post)
    sample = str(post.get("large_file_url") or preview)
    tags = str(post.get("tag_string") or "")
    return {
        "source": "danbooru",
        "id": str(pid),
        "key": f"danbooru:{pid}",
        "width": int(post.get("image_width") or 0),
        "height": int(post.get("image_height") or 0),
        "score": int(post.get("score") or 0),
        "tags": tags,
        "preview_url": preview,
        "sample_url": sample,
        "file_url": file_url,
        "rating": str(post.get("rating") or ""),
        "file_ext": str(post.get("file_ext") or ""),
    }


async def search_gelbooru(
    tags: str,
    *,
    page: int,
    proxy: str | None,
) -> tuple[list[dict[str, Any]], bool]:
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

    out: list[dict[str, Any]] = []
    for post in posts_raw:
        n = normalize_gelbooru_post(post)
        if n:
            out.append(n)
    return out, has_more


async def search_danbooru(
    tags: str,
    *,
    page: int,
    limit: int,
    proxy: str | None,
) -> tuple[list[dict[str, Any]], bool]:
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
    out: list[dict[str, Any]] = []
    for post in data:
        if not isinstance(post, dict):
            continue
        n = normalize_danbooru_post(post)
        if n:
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
) -> tuple[list[dict[str, Any]], bool]:
    source = source.strip().lower()
    if source not in SOURCES:
        raise ValueError(f"不支持的源: {source}")
    query = build_search_query(source, tags, order=order, extra=extra)
    if source == "gelbooru":
        return await search_gelbooru(query, page=page, proxy=proxy)
    return await search_danbooru(query, page=page, limit=limit, proxy=proxy)


async def fetch_preview_bytes(
    source: str,
    post_id: str,
    *,
    proxy: str | None,
) -> tuple[bytes, str]:
    source = source.strip().lower()
    if source == "gelbooru":
        api = make_gelbooru(proxy)
        post = await api.getPost(int(post_id))
        url = str(post.get("preview_url") or post.get("sample_url") or post.get("file_url") or "")
        if not url:
            raise ValueError("无预览地址")
        headers = _headers(referer="https://gelbooru.com/")
        async with httpx.AsyncClient(
            proxy=_proxy_or_none(proxy), timeout=30.0, headers=headers
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type") or "image/jpeg"
            return resp.content, ctype.split(";")[0].strip()

    if source == "danbooru":
        meta = await _danbooru_get(
            f"https://danbooru.donmai.us/posts/{post_id}.json",
            proxy=proxy,
            accept="application/json, text/plain, */*",
            timeout=30.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}: {meta.text[:200]}")
        post = meta.json()
        preview = danbooru_preview_url(post)
        if not preview:
            raise ValueError("无预览地址")
        img = await _danbooru_get(
            preview,
            proxy=proxy,
            accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            timeout=30.0,
        )
        if img.status_code != 200:
            raise RuntimeError(f"预览 HTTP {img.status_code}")
        ctype = img.headers.get("content-type") or "image/jpeg"
        return img.content, ctype.split(";")[0].strip()

    raise ValueError(f"不支持的源: {source}")


async def fetch_sample_bytes(
    source: str,
    post_id: str,
    *,
    proxy: str | None,
) -> tuple[bytes, str]:
    """大图预览：优先 sample / large，再退回 file。"""
    source = source.strip().lower()
    if source == "gelbooru":
        api = make_gelbooru(proxy)
        post = await api.getPost(int(post_id))
        url = str(
            post.get("sample_url")
            or post.get("file_url")
            or post.get("preview_url")
            or ""
        )
        if not url:
            raise ValueError("无大图地址")
        headers = _headers(referer="https://gelbooru.com/")
        async with httpx.AsyncClient(
            proxy=_proxy_or_none(proxy), timeout=45.0, headers=headers
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type") or "image/jpeg"
            return resp.content, ctype.split(";")[0].strip()

    if source == "danbooru":
        meta = await _danbooru_get(
            f"https://danbooru.donmai.us/posts/{post_id}.json",
            proxy=proxy,
            accept="application/json, text/plain, */*",
            timeout=30.0,
        )
        if meta.status_code != 200:
            raise RuntimeError(f"Danbooru HTTP {meta.status_code}: {meta.text[:200]}")
        post = meta.json()
        url = str(
            post.get("large_file_url")
            or post.get("file_url")
            or danbooru_preview_url(post)
            or ""
        )
        if not url:
            raise ValueError("无大图地址")
        img = await _danbooru_get(
            url,
            proxy=proxy,
            accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            timeout=45.0,
        )
        if img.status_code != 200:
            raise RuntimeError(f"大图 HTTP {img.status_code}")
        ctype = img.headers.get("content-type") or "image/jpeg"
        return img.content, ctype.split(";")[0].strip()

    raise ValueError(f"不支持的源: {source}")


async def download_post(
    source: str,
    post_id: str,
    out_dir: Path,
    *,
    proxy: str | None,
    overwrite: bool = False,
) -> dict[str, Any]:
    source = source.strip().lower()
    if source not in SOURCES:
        raise ValueError(f"不支持的源: {source}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pid = str(post_id).strip()
    stem = file_stem(source, pid)
    image_path = out_dir / f"{stem}.png"
    tags_path = out_dir / f"{stem}.txt"
    existing = find_downloaded(out_dir, source, pid)

    if source == "gelbooru":
        api = make_gelbooru(proxy)
        if not overwrite and existing is not None and tags_path.is_file():
            return {
                "skipped": True,
                "path": str(existing),
                "tags_path": str(tags_path),
                "id": pid,
                "stem": stem,
                "source": source,
            }
        post = await api.getPost(int(pid))
        file_url = str(post.get("file_url") or "")
        if not file_url:
            raise ValueError("无 file_url")
        if overwrite or not image_path.is_file():
            tmp_ext = _ext_from_url(file_url, ".bin")
            tmp_path = out_dir / f"{stem}.tmp{tmp_ext}"
            post = await api.getImage(int(pid), str(tmp_path))
            _convert_path_to_png(tmp_path, image_path)
            # 清掉旧版纯 id / 其它后缀
            for old in _stem_candidates(source, pid):
                for ext in _IMAGE_EXTS:
                    p = out_dir / f"{old}{ext}"
                    if p.is_file() and p.resolve() != image_path.resolve():
                        p.unlink()
        caption = _tags_to_caption(str(post.get("tags") or ""))
        tags_path.write_text(caption + "\n", encoding="utf-8")
        # 旁路旧名 txt
        legacy_txt = out_dir / f"{pid}.txt"
        if legacy_txt.is_file() and legacy_txt.resolve() != tags_path.resolve():
            legacy_txt.unlink()
        _write_meta(out_dir, source, pid, post)
        return {
            "skipped": False,
            "path": str(image_path),
            "tags_path": str(tags_path),
            "id": pid,
            "stem": stem,
            "source": source,
        }

    meta = await _danbooru_get(
        f"https://danbooru.donmai.us/posts/{pid}.json",
        proxy=proxy,
        accept="application/json, text/plain, */*",
        timeout=60.0,
    )
    if meta.status_code != 200:
        raise RuntimeError(f"Danbooru HTTP {meta.status_code}: {meta.text[:200]}")
    post = meta.json()
    file_ext = str(post.get("file_ext") or "").lower().lstrip(".")
    # zip/视频：若有 large 仍可下静态图；否则直接拒绝
    if file_ext in _UNSUPPORTED_FILE_EXT and not post.get("large_file_url"):
        raise ValueError(f"不支持的类型: .{file_ext}")
    if not overwrite and existing is not None and tags_path.is_file():
        _write_meta(out_dir, source, pid, post)
        return {
            "skipped": True,
            "path": str(existing),
            "tags_path": str(tags_path),
            "id": pid,
            "stem": stem,
            "source": source,
        }
    data = await _danbooru_fetch_image_bytes(post, proxy=proxy)
    _save_image_as_png(data, image_path)
    for old in _stem_candidates(source, pid):
        for ext in _IMAGE_EXTS:
            p = out_dir / f"{old}{ext}"
            if p.is_file() and p.resolve() != image_path.resolve():
                p.unlink()
    caption = _tags_to_caption(str(post.get("tag_string") or ""))
    tags_path.write_text(caption + "\n", encoding="utf-8")
    legacy_txt = out_dir / f"{pid}.txt"
    if legacy_txt.is_file() and legacy_txt.resolve() != tags_path.resolve():
        legacy_txt.unlink()
    _write_meta(out_dir, source, pid, post)
    return {
        "skipped": False,
        "path": str(image_path),
        "tags_path": str(tags_path),
        "id": pid,
        "stem": stem,
        "source": source,
    }


def local_rel_path(root: Path, abs_path: str | Path) -> str:
    p = Path(abs_path).resolve()
    return str(p.relative_to(root.resolve())).replace("\\", "/")
