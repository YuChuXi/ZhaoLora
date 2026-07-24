"""Booru 预览拉取、下载与取消下载。

引用: ``routes.booru``。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import httpx

from services.models import BooruDownloadResult, BooruUndownloadResult
from services.scanner import IMAGE_EXTS
from services.booru_client.common import (
    META_SUBDIR,
    SOURCES,
    STEM_PREFIX,
    _UNSUPPORTED_FILE_EXT,
    _convert_path_to_png,
    _curl_proxies,
    _danbooru_get,
    _ext_from_url,
    _find_existing,
    _headers,
    _pil_can_open,
    _proxy_or_none,
    _save_image_as_png,
    _sniff_image_kind,
    _stem_candidates,
    _tags_to_caption,
    _write_meta,
    danbooru_preview_url,
    file_stem,
    find_downloaded,
    make_gelbooru,
)

async def _danbooru_fetch_image_bytes(
    post: Mapping[str, object],
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

    return BooruUndownloadResult(
        id=pid,
        source=source,
        removed=removed,
        count=len(removed),
    )

async def fetch_preview_bytes(
    source: str,
    post_id: str,
    *,
    proxy: str | None,
) -> tuple[bytes, str]:
    """拉取预览图字节。引用：routes.booru.booru_thumb。"""
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
) -> BooruDownloadResult:
    """下载帖子原图为 PNG，并写旁路标签与 meta。

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
    existing = find_downloaded(out_dir, source, pid)

    if source == "gelbooru":
        api = make_gelbooru(proxy)
        if not overwrite and existing is not None and tags_path.is_file():
            return BooruDownloadResult(
                skipped=True,
                path=str(existing),
                tags_path=str(tags_path),
                id=pid,
                stem=stem,
                source=source,
            )
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
                for ext in IMAGE_EXTS:
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
        return BooruDownloadResult(
            skipped=False,
            path=str(image_path),
            tags_path=str(tags_path),
            id=pid,
            stem=stem,
            source=source,
        )

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
        return BooruDownloadResult(
            skipped=True,
            path=str(existing),
            tags_path=str(tags_path),
            id=pid,
            stem=stem,
            source=source,
        )
    data = await _danbooru_fetch_image_bytes(post, proxy=proxy)
    _save_image_as_png(data, image_path)
    for old in _stem_candidates(source, pid):
        for ext in IMAGE_EXTS:
            p = out_dir / f"{old}{ext}"
            if p.is_file() and p.resolve() != image_path.resolve():
                p.unlink()
    caption = _tags_to_caption(str(post.get("tag_string") or ""))
    tags_path.write_text(caption + "\n", encoding="utf-8")
    legacy_txt = out_dir / f"{pid}.txt"
    if legacy_txt.is_file() and legacy_txt.resolve() != tags_path.resolve():
        legacy_txt.unlink()
    _write_meta(out_dir, source, pid, post)
    return BooruDownloadResult(
        skipped=False,
        path=str(image_path),
        tags_path=str(tags_path),
        id=pid,
        stem=stem,
        source=source,
    )


def local_rel_path(root: Path, abs_path: str | Path) -> str:
    """绝对路径转数据集相对路径。引用：routes.booru.booru_download。"""
    p = Path(abs_path).resolve()
    return str(p.relative_to(root.resolve())).replace("\\", "/")

