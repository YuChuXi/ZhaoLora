"""Booru 图片/文件/标签工具. 

Callers:
    - ``common.SourceClient``
    - ``sources``
    - ``media``
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from services.core.scanner import IMAGE_EXTS


def sniff_image_kind(data: bytes) -> str:
    if not data: return "empty"
    if data[:3] == b"\xff\xd8\xff": return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n": return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"): return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis", b"mif1", b"msf1", b"heic", b"heif"): return "avif"
        return "video"
    if data[:4] == b"PK\x03\x04": return "zip"
    if data[:4] == b"\x1aE\xdf\xa3": return "webm"
    head = data[:80].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<?xml")): return "html"
    return "unknown"


def pil_can_open(data: bytes) -> bool:
    from PIL import UnidentifiedImageError
    try:
        im = Image.open(io.BytesIO(data)); im.load()
        return True
    except UnidentifiedImageError:
        return False


def stem_candidates(post_id: str, *, file_stem_fn) -> list[str]:
    """生成候选文件名 stem: 规范名 + 纯 id. 

    Args:
        post_id: 帖子 ID. 
        file_stem_fn: 接受单个 id 返回 stem 的函数(绑定方法), 如 ``SourceClient.file_stem``. 

    Callers:
        - `base.py:SourceClient._clean_old_files`
        - `media.py:remove_download`
        - `media.py:download_post`
    """
    pid = str(post_id).strip()
    primary = file_stem_fn(pid)
    return [primary] if pid == primary else [primary, pid]


def find_downloaded(out_dir: Path, filename: str) -> Path | None:
    """查找已下载帖图. 以完整文件名（含扩展名）判断, 无需探测扩展名.

    Args:
        filename: 完整文件名，如 ``d12345.png``（来源在 search 阶段提供真实扩展名）.

    Callers:
        - `danbooru/source.py:DanbooruSource.download`
        - `gelbooru/source.py:GelbooruSource.download`
        - `pixiv/source.py:PixivSource.download`
        - `bilibili/source.py:BilibiliSource.download`
        - `gallerydl.py:download`
    """
    p = out_dir / filename
    return p if p.is_file() else None


def is_downloaded(out_dir: Path, filename: str) -> bool:
    """是否已下载.

    Args:
        filename: 完整文件名，如 ``d12345.png``.

    Callers:
        - `routes/online_sources.py:search`
        - `services/online_sources/batch.py`
    """
    return find_downloaded(out_dir, filename) is not None


_GENDER_TAGS: frozenset[str] = frozenset({
    "1girl", "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple_girls",
    "1boy", "2boys", "3boys", "4boys", "5boys", "6+boys", "multiple_boys",
    "1other", "2others", "3others", "4others", "5others", "6+others", "multiple_others",
    "solo", "no_humans",
})


def _tags_to_caption(tags: str) -> str:
    parts = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    return ", ".join(parts)
