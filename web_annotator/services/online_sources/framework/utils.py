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


def find_downloaded(out_dir: Path, post_id: str, *, file_stem_fn) -> Path | None:
    """查找已下载帖图. 下载输出统一为 {stem}.png, 文件名确定, 无需探测扩展名.

    Args:
        post_id: 帖子 ID. 
        file_stem_fn: 接受单个 id 返回 stem 的函数(绑定方法), 如 ``SourceClient.file_stem``. 

    Callers:
        - `gelbooru/source.py:GelbooruSource.download`
        - `danbooru/source.py:DanbooruSource.download`
    """
    p = out_dir / f"{file_stem_fn(post_id)}.png"
    return p if p.is_file() else None


def is_downloaded(out_dir: Path, post_id: str, *, file_stem_fn) -> bool:
    """是否已下载. 

    Args:
        post_id: 帖子 ID. 
        file_stem_fn: 接受单个 id 返回 stem 的函数(绑定方法), 如 ``SourceClient.file_stem``. 

    Callers:
        - `routes/online_sources.py:search`
        - `services/online_sources/batch.py`
    """
    return find_downloaded(out_dir, post_id, file_stem_fn=file_stem_fn) is not None


_GENDER_TAGS: frozenset[str] = frozenset({
    "1girl", "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple_girls",
    "1boy", "2boys", "3boys", "4boys", "5boys", "6+boys", "multiple_boys",
    "1other", "2others", "3others", "4others", "5others", "6+others", "multiple_others",
    "solo", "no_humans",
})


def _tags_to_caption(tags: str) -> str:
    parts = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    return ", ".join(parts)


# ---- 图片转换(供 sources / media 使用) ----
def convert_path_to_png(src: Path, dest: Path) -> None:
    """临时文件转 PNG. 

    Callers:
        - `gelbooru/source.py:GelbooruSource.download`
    """
    from services.online_sources.framework.base import SourceClient
    data = src.read_bytes()
    if src.is_file():
        src.unlink()
    # 使用 SourceClient._save_png 需要实例, 这里直接用内联逻辑
    kind = sniff_image_kind(data)
    if kind in ("html", "zip", "video", "webm"):
        raise ValueError(f"不支持该文件类型({kind})")
    if kind == "png":
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return
    img = Image.open(io.BytesIO(data))
    img.load()
    out = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(dest, format="PNG", optimize=True)
