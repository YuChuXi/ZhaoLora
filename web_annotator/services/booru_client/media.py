"""Booru 预览拉取、下载与取消下载。

引用: ``routes.booru``。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import httpx

from services.models import BooruPost, BooruDownloadResult, BooruUndownloadResult
from services.scanner import IMAGE_EXTS
from services.booru_client.common import (
    _SOURCES as SOURCES,
    build_caption_from_meta,
    file_stem,
    get_source,
)
from services.booru_client.net import (
    UNSUPPORTED_FILE_EXT,
    danbooru_get,
    ext_from_url,
    make_gelbooru,
    make_headers,
    proxy_or_none,
)
from services.booru_client.utils import (
    convert_path_to_png,
    find_downloaded,
    find_existing,
    pil_can_open,
    sniff_image_kind,
    stem_candidates,
)

_CACHE_ROOT = Path(tempfile.gettempdir()) / "zhao_booru_media"
_clients: dict[str, httpx.AsyncClient] = {}
_client_lock = asyncio.Lock()
_inflight: dict[str, asyncio.Future[tuple[bytes, str]]] = {}
_inflight_lock = asyncio.Lock()
_tag_types_cache: dict[str, int] = {}


def _cache_files(kind: str, post_id: str) -> tuple[Path, Path]:
    """磁盘缓存路径。

    Callers:
        - `media.py:fetch_gelbooru_bytes`
    """
    base = _CACHE_ROOT / kind / post_id
    return base.with_suffix(".bin"), base.with_suffix(".ctype")


def _assert_gelbooru_url(url: str) -> str:
    """校验 Gelbooru 图床 URL。

    Callers:
        - `media.py:fetch_gelbooru_bytes`
    """
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("非法媒体地址")
    host = u.hostname.lower()
    if host != "gelbooru.com" and not host.endswith(".gelbooru.com"):
        raise ValueError("非 Gelbooru 图床")
    return url.strip()


async def _shared_client(proxy: str | None) -> httpx.AsyncClient:
    """按代理复用 HTTP 连接池。

    Callers:
        - `media.py:fetch_gelbooru_bytes`
    """
    key = proxy_or_none(proxy) or ""
    c = _clients.get(key)
    if c is not None and not c.is_closed:
        return c
    async with _client_lock:
        c = _clients.get(key)
        if c is not None and not c.is_closed:
            return c
        c = httpx.AsyncClient(
            proxy=proxy_or_none(proxy),
            headers=make_headers(referer="https://gelbooru.com/"),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            timeout=httpx.Timeout(45.0, connect=10.0),
        )
        _clients[key] = c
        return c


async def _download_gelbooru(
    kind: str,
    pid: str,
    media_url: str,
    *,
    proxy: str | None,
) -> tuple[bytes, str]:
    """从 CDN 下载并写入磁盘缓存。

    Callers:
        - `media.py:fetch_gelbooru_bytes`
    """
    timeout = 30.0 if kind == "thumb" else 45.0
    client = await _shared_client(proxy)
    resp = await client.get(media_url, timeout=timeout)
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    data = resp.content
    bin_path, ctype_path = _cache_files(kind, pid)
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_bytes(data)
    ctype_path.write_text(ctype, encoding="utf-8")
    return data, ctype


async def fetch_gelbooru_bytes(
    kind: str,
    post_id: str,
    *,
    url: str,
    proxy: str | None,
) -> tuple[bytes, str]:
    """拉 Gelbooru thumb/sample：磁盘缓存 + 同图进行中合并。

    引用:
        ``routes.booru.booru_thumb`` / ``booru_sample``。
    """
    if kind not in ("thumb", "sample"):
        raise ValueError(f"不支持的 kind: {kind}")
    pid = str(post_id).strip()
    if not pid or "/" in pid or "\\" in pid or ".." in pid:
        raise ValueError("非法 id")
    media_url = _assert_gelbooru_url(url)

    bin_path, ctype_path = _cache_files(kind, pid)
    if bin_path.is_file() and ctype_path.is_file():
        return bin_path.read_bytes(), ctype_path.read_text(encoding="utf-8").strip()

    flight_key = f"{kind}:{pid}"
    async with _inflight_lock:
        existing = _inflight.get(flight_key)
        if existing is not None:
            leader = False
            fut = existing
        else:
            leader = True
            fut = asyncio.get_running_loop().create_future()
            _inflight[flight_key] = fut

    if not leader:
        return await fut

    try:
        result = await _download_gelbooru(kind, pid, media_url, proxy=proxy)
        fut.set_result(result)
        return result
    except Exception as e:
        fut.set_exception(e)
        raise
    finally:
        async with _inflight_lock:
            if _inflight.get(flight_key) is fut:
                del _inflight[flight_key]


async def _danbooru_fetch_image_bytes(
    post: BooruPost,
    *,
    proxy: str | None,
) -> bytes:
    """优先原图；失败或不支持时回退 large_file_url。"""
    urls: list[str] = [u for u in (post.file_url, post.large_file_url) if u and u not in (None, "")]
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise ValueError("该帖无 file_url（可能需登录或已删除）")

    last_err = "无法下载图片"
    for url in urls:
        resp = await danbooru_get(
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
        kind = sniff_image_kind(data)
        if kind in ("empty", "html", "zip", "video", "webm"):
            last_err = f"不支持该文件类型（{kind}）"
            continue
        if kind == "unknown" and not pil_can_open(data):
            last_err = f"无法识别的图片数据（大小={len(data)}字节）"
            continue
        return data
    raise ValueError(last_err)


def remove_download(
    source: str,
    post_id: str,
    out_dir: Path,
) -> BooruUndownloadResult:
    """取消选中时删除已下载的图、旁路 txt 与 meta。

    引用:
        ``routes.booru.booru_undownload``。
    """
    source = source.strip().lower()
    if source not in SOURCES:
        raise ValueError(f"不支持的源: {source}")
    pid = str(post_id).strip()
    # 若误传 g123 / d123，剥掉前缀
    src = get_source(source)
    prefix = src.prefix
    if prefix and pid.startswith(prefix) and pid[len(prefix) :].isdigit():
        pid = pid[len(prefix) :]
    if not pid or "/" in pid or "\\" in pid or ".." in pid:
        raise ValueError("非法 id")

    removed: list[str] = []
    for stem in stem_candidates(source, pid, file_stem_fn=src.file_stem):
        img = find_existing(out_dir, stem)
        if img is not None:
            img.unlink()
            removed.append(img.name)
        tags_path = out_dir / f"{stem}.txt"
        if tags_path.is_file():
            tags_path.unlink()
            removed.append(tags_path.name)

    meta_path = out_dir / src.meta_subdir / f"{pid}.json"
    if meta_path.is_file():
        meta_path.unlink()
        removed.append(f"{src.meta_subdir}/{pid}.json")

    return BooruUndownloadResult(
        id=pid,
        source=source,
        removed=removed,
        count=len(removed),
    )


async def _categorize_gelbooru_tags(post: dict, api) -> dict:
    """查询 Gelbooru 标签类型并写入 Danbooru 兼容字段，结果缓存在内存。

    Args:
        post: Gelbooru API 返回的帖子数据。
        api: Gelbooru 客户端实例。

    Returns:
        含 tag_string_* 字段的新 dict。

    Callers:
        - `media.py:download_post`
    """
    all_names = str(post.get("tags") or "").split()
    if not all_names:
        return dict(post)
    missing = [n for n in all_names if n not in _tag_types_cache]
    if missing:
        types = await api.getTagTypes(missing)
        _tag_types_cache.update(types)
    cats: dict[int, list[str]] = {0: [], 1: [], 3: [], 4: [], 5: []}
    for t in all_names:
        cats[_tag_types_cache.get(t, 0)].append(t)
    post = dict(post)
    post["tag_string_general"] = " ".join(cats[0])
    post["tag_string_artist"] = " ".join(cats[1])
    post["tag_string_copyright"] = " ".join(cats[3])
    post["tag_string_character"] = " ".join(cats[4])
    post["tag_string_meta"] = " ".join(cats[5])
    return post


async def download_post(
    source: str,
    post_id: str,
    out_dir: Path,
    *,
    proxy: str | None,
    overwrite: bool = False,
    anima_format: bool = True,
    include_meta_tags: bool = False,
    keep_underscores: bool = False,
) -> BooruDownloadResult:
    """下载帖子原图为 PNG，并写旁路标签与 meta。

    Args:
        anima_format: 按 Anima 格式输出 [性别][角色][系列][画师][通用标签]。
        include_meta_tags: 若为 True，保留元数据标签（rating、分辨率等）；默认不保留。
        keep_underscores: 若为 True，保留下划线；默认转为空格。

    引用:
        ``routes.booru.booru_download``。
    """
    source = source.strip().lower()
    if source not in SOURCES:
        raise ValueError(f"不支持的源: {source}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pid = str(post_id).strip()
    stem = file_stem(source, pid)
    image_path = out_dir / f"{stem}.png"
    tags_path = out_dir / f"{stem}.txt"
    existing = find_downloaded(out_dir, source, pid, file_stem_fn=get_source(source).file_stem)

    if source == "gelbooru":
        api = make_gelbooru(proxy)
        if not overwrite and existing is not None and tags_path.is_file():
            return BooruDownloadResult(
                skipped=True,
                path=str(existing),
                tags_path=str(tags_path),
                id=pid,
                stem=stem,
                source=source
            )
        post = await api.getPost(int(pid))
        api_post = BooruPost.model_validate(post)
        file_url = api_post.file_url
        if not file_url:
            raise ValueError("无 file_url")
        if overwrite or not image_path.is_file():
            tmp_ext = ext_from_url(file_url, ".bin")
            tmp_path = out_dir / f"{stem}.tmp{tmp_ext}"
            _image_result = await api.getImage(int(pid), str(tmp_path))
            convert_path_to_png(tmp_path, image_path)
            # 清掉旧版纯 id / 其它后缀
            for old in stem_candidates(source, pid, file_stem_fn=get_source(source).file_stem):
                for ext in IMAGE_EXTS:
                    p = out_dir / f"{old}{ext}"
                    if p.is_file() and p.resolve() != image_path.resolve():
                        p.unlink()
        # Gelbooru Anima 格式：查询标签类型，构造 Danbooru 兼容字段
        if anima_format and source == "gelbooru":
            post_dict = await _categorize_gelbooru_tags(api_post.model_dump(), api)
            api_post = BooruPost.model_validate(post_dict)
        caption = build_caption_from_meta(api_post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
        tags_path.write_text(caption + "\n", encoding="utf-8")
        get_source(source)._write_meta(out_dir, pid, api_post)
        return BooruDownloadResult(
            skipped=False,
            path=str(image_path),
            tags_path=str(tags_path),
            id=pid,
            stem=stem,
            source=source
        )

    meta = await danbooru_get(
        f"https://danbooru.donmai.us/posts/{pid}.json",
        proxy=proxy,
        accept="application/json, text/plain, */*",
        timeout=60.0,
    )
    if meta.status_code != 200:
        raise RuntimeError(f"Danbooru HTTP {meta.status_code}: {meta.text[:200]}")
    post = meta.json()
    api_post = BooruPost.model_validate(post)
    file_ext = api_post.file_ext
    if file_ext in UNSUPPORTED_FILE_EXT and not api_post.large_file_url:
        raise ValueError(f"不支持的类型: .{file_ext}")
    if not overwrite and existing is not None and tags_path.is_file():
        get_source(source)._write_meta(out_dir, pid, api_post)
        return BooruDownloadResult(
            skipped=True,
            path=str(existing),
            tags_path=str(tags_path),
            id=pid,
            stem=stem,
            source=source
        )
    data = await _danbooru_fetch_image_bytes(api_post, proxy=proxy)
    tmp_path = out_dir / f"{stem}.tmp"
    tmp_path.write_bytes(data)
    convert_path_to_png(tmp_path, image_path)
    for old in stem_candidates(source, pid, file_stem_fn=get_source(source).file_stem):
        for ext in IMAGE_EXTS:
            p = out_dir / f"{old}{ext}"
            if p.is_file() and p.resolve() != image_path.resolve():
                p.unlink()
    caption = build_caption_from_meta(api_post, anima_format=anima_format, include_meta_tags=include_meta_tags, keep_underscores=keep_underscores)
    tags_path.write_text(caption + "\n", encoding="utf-8")
    legacy_txt = out_dir / f"{pid}.txt"  # Danbooru
    if legacy_txt.is_file() and legacy_txt.resolve() != tags_path.resolve():
        legacy_txt.unlink()
    get_source(source)._write_meta(out_dir, pid, api_post)
    return BooruDownloadResult(
        skipped=False,
        path=str(image_path),
        tags_path=str(tags_path),
        id=pid,
        stem=stem,
        source=source
    )


def local_rel_path(root: Path, abs_path: str | Path) -> str:
    """绝对路径转数据集相对路径。

    Callers:
        - `routes/booru.py:booru_download`
    """
    p = Path(abs_path).resolve()
    return str(p.relative_to(root.resolve())).replace("\\", "/")


async def regenerate_captions(
    out_dir: Path,
    *,
    anima_format: bool = True,
    include_meta_tags: bool = False,
    keep_underscores: bool = False,
) -> int:
    """从 .danbooru_meta / .gelbooru_meta 批量重生成旁路 .txt。

    Args:
        out_dir: 图片与元数据所在目录。
        anima_format: Anima 格式输出。
        include_meta_tags: 是否包含元数据标签。
        keep_underscores: 是否保留下划线。

    Returns:
        成功重生成的 txt 数量。

    Callers:
        - `routes/booru.py:booru_regenerate_captions`
    """
    from services.booru_client.common import (
        META_SUBDIR, build_caption_from_meta, file_stem,
    )

    import json

    count = 0
    for source in ("danbooru", "gelbooru"):
        meta_dir = out_dir / META_SUBDIR.get(source, "")
        if not meta_dir.is_dir():
            continue

        for meta_file in sorted(meta_dir.glob("*.json")):
            try:
                post = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            pid = str(post.get("id") or meta_file.stem)
            stem = file_stem(source, pid)
            txt_path = out_dir / f"{stem}.txt"

            caption = build_caption_from_meta(
                post, anima_format=anima_format,
                include_meta_tags=include_meta_tags,
                keep_underscores=keep_underscores,
            )
            txt_path.write_text(caption + "\n", encoding="utf-8")

            legacy_txt = out_dir / f"{pid}.txt"
            if legacy_txt.is_file() and legacy_txt.resolve() != txt_path.resolve():
                legacy_txt.unlink()

            count += 1

    return count
