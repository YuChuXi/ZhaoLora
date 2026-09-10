"""Booru 图片/文件/标签工具. 

Callers:
    - ``services/online_sources/framework/base.py:SourceClient``
    - ``services/online_sources/__init__.py:`` 模块
    - ``services/online_sources/framework/media.py:`` 模块
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from services.core.atomic import atomic_write_bytes


def sniff_image_kind(data: bytes) -> str:
    """按魔数判定字节流的文件类型.

    Args:
        data: 文件头部字节 (建议至少 80 字节).

    Returns:
        str: 类型标识, 取值 ``jpeg``/``png``/``gif``/``webp``/``avif``/
        ``video``/``zip``/``webm``/``html``/``empty``/``unknown``.

    Callers:
        - ``services/online_sources/framework/utils.py:save_png_bytes``
        - ``services/online_sources/danbooru/source.py:DanbooruSource._fetch_image_bytes``
    """
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
    """探测字节流能否被 PIL 完整解码为图片.

    Args:
        data: 完整文件字节.

    Returns:
        bool: 可解码为 ``True``; PIL 无法识别 (``UnidentifiedImageError``)
        为 ``False``. 其他异常 (如截断的 OSError) 原样抛出.

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource._fetch_image_bytes``
    """
    from PIL import UnidentifiedImageError
    try:
        im = Image.open(io.BytesIO(data)); im.load()
        return True
    except UnidentifiedImageError:
        return False


def save_png_bytes(data: bytes, dest: Path) -> None:
    """将图片字节按 PNG 写入磁盘, 并校验其确为可识别图片.

    校验规则: 先按魔数排除 html/zip/video/webm 等非图片; PNG 原样写入;
    其余格式经 PIL 解码重编码为 PNG。PIL 无法识别或解码失败时抛出异常,
    保证磁盘上不会出现以图片扩展名存放的非图片数据。

    Args:
        data: 原始字节.
        dest: 目标 PNG 路径 (父目录不存在时自动创建).

    Raises:
        ValueError: 非图片类型 (html/zip/video/webm).
        UnidentifiedImageError / OSError: 数据无法被 PIL 解码.

    Callers:
        - ``services/online_sources/framework/base.py:SourceClient._save_png_sync``
        - ``services/online_sources/gelbooru/client.py:GelbooruClient.get_image``
    """
    kind = sniff_image_kind(data)
    if kind in ("html", "zip", "video", "webm"):
        raise ValueError(f"不支持该文件类型({kind})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "png":
        atomic_write_bytes(dest, data)
        return
    img = Image.open(io.BytesIO(data))
    img.load()
    out = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    out.save(dest, format="PNG", optimize=True)


def stem_candidates(post_id: str, *, file_stem_fn) -> list[str]:
    """生成候选文件名 stem: 规范名 + 纯 id. 

    Args:
        post_id: 帖子 ID. 
        file_stem_fn: 接受单个 id 返回 stem 的函数(绑定方法), 如 ``SourceClient.file_stem``. 

    Callers:
        - ``services/online_sources/framework/base.py:SourceClient._clean_old_files``
        - ``services/online_sources/framework/media.py:remove_download``
        - ``services/online_sources/framework/media.py:download_post``
    """
    pid = str(post_id).strip()
    primary = file_stem_fn(pid)
    return [primary] if pid == primary else [primary, pid]


def find_downloaded(out_dir: Path, filename: str) -> Path | None:
    """查找已下载帖图. 以完整文件名（含扩展名）判断, 无需探测扩展名.

    Args:
        filename: 完整文件名，如 ``d12345.png``（来源在 search 阶段提供真实扩展名）.

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource.download``
        - ``services/online_sources/gelbooru/source.py:GelbooruSource.download``
        - ``services/online_sources/pixiv/source.py:PixivSource.download``
        - ``services/online_sources/bilibili/source.py:BilibiliSource.download``
        - ``services/online_sources/framework/gallerydl.py:download``
    """
    p = out_dir / filename
    return p if p.is_file() else None


def is_downloaded(out_dir: Path, filename: str) -> bool:
    """是否已下载.

    Args:
        filename: 完整文件名，如 ``d12345.png``.

    Callers:
        - ``routes/online_sources.py:search``
        - ``services/online_sources/batch.py:``
    """
    return find_downloaded(out_dir, filename) is not None


_GENDER_TAGS: frozenset[str] = frozenset({
    "1girl", "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple_girls",
    "1boy", "2boys", "3boys", "4boys", "5boys", "6+boys", "multiple_boys",
    "1other", "2others", "3others", "4others", "5others", "6+others", "multiple_others",
    "solo", "no_humans",
})


def _tags_to_caption(tags: str) -> str:
    """把空格/逗号分隔的标签串转为逗号+空格分隔的标注文本.

    Args:
        tags: 空格/逗号分隔的标签串.

    Returns:
        str: ``, `` 连接的标注文本.

    Callers:
        - ``services/online_sources/framework/base.py:build_caption``
    """
    parts = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    return ", ".join(parts)
