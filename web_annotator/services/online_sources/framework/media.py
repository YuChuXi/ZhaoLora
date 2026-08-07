"""Booru 下载、取消下载与标签重生成.

引用: ``routes.online_sources``.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.models import BooruDownloadResult, BooruPost, BooruUndownloadResult
from services.online_sources.framework.base import (
    META_SUBDIR,
    _SOURCES as SOURCES,
    build_caption_from_meta,
    file_stem,
    get_source,
)
from services.online_sources.framework.utils import (
    find_downloaded,
)


def remove_download(
    source: str,
    filename: str,
    out_dir: Path,
) -> BooruUndownloadResult:
    """取消选中时删除已下载的图、旁路 txt 与 meta.

    以完整文件名(如 ``d12345.png``)为参数, 由文件名反推 stem 与 pid, 无需探测.

    Callers:
        - ``routes.online_sources.booru_undownload``
    """
    source = source.strip().lower()
    if source not in SOURCES:
        raise ValueError(f"不支持的源: {source}")
    src = get_source(source)
    prefix = src.prefix

    img = out_dir / filename
    stem = img.stem  # 去掉后缀, 如 d12345
    pid = stem[len(prefix):] if prefix and stem.startswith(prefix) else stem

    removed: list[str] = []
    images: list[str] = []  # 被删图片文件名, 供缩略图清理
    if img.is_file():
        img.unlink()
        removed.append(img.name)
        images.append(img.name)
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
        images=images,
        count=len(removed),
    )


async def download_post(
    source: str,
    post: BooruPost,
    out_dir: Path,
    *,
    overwrite: bool = False,
    anima_format: bool = True,
    include_meta_tags: bool = False,
    keep_underscores: bool = False,
) -> BooruDownloadResult:
    """下载帖子原图为 PNG, 并写旁路标签与 meta.

    全部委托给对应 ``SourceClient.download`` 实现, 本层只做前缀校验与
    结果翻译。

    Args:
        source: 来源名。
        post: 前端搜索阶段已持有的帖子 (含原图 URL 与标签)。
        out_dir: 输出目录。
        overwrite: 是否覆盖已下载文件。
        anima_format: 按 Anima 格式输出标签。
        include_meta_tags: 是否保留元数据标签。
        keep_underscores: 是否保留下划线。

    Returns:
        BooruDownloadResult: 下载结果。

    Callers:
        - ``routes.online_sources.booru_download``
    """
    source = source.strip().lower()
    if source not in SOURCES:
        raise ValueError(f"不支持的源: {source}")
    out_dir.mkdir(parents=True, exist_ok=True)

    result = await get_source(source).download(
        post, out_dir,
        overwrite=overwrite,
        anima_format=anima_format,
        include_meta_tags=include_meta_tags,
        keep_underscores=keep_underscores,
    )
    return BooruDownloadResult(source=source, **result)


def local_rel_path(root: Path, abs_path: str | Path) -> str:
    """绝对路径转数据集相对路径.

    Callers:
        - ``routes/online_sources.py:download``
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
    """从 .danbooru_meta / .gelbooru_meta 批量重生成旁路 .txt.

    Args:
        out_dir: 图片与元数据所在目录.
        anima_format: Anima 格式输出.
        include_meta_tags: 是否包含元数据标签.
        keep_underscores: 是否保留下划线.

    Returns:
        成功重生成的 txt 数量.

    Callers:
        - ``routes/online_sources.py:regenerate_captions``
    """
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