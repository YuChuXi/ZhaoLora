"""Booru 下载、取消下载与标签重生成.

Callers:
    - ``routes/online_sources.py:``
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from services.core.natural import natural_key
from services.core.video_scanner import VIDEO_EXTS
from services.models import BooruDownloadResult, BooruPost, BooruUndownloadResult
from services.online_sources.framework.base import (
    META_SUBDIR,
    ProgressFn,
    _SOURCES as SOURCES,
    build_caption_from_meta,
    file_stem,
    get_source,
)
from services.online_sources.framework.http import HttpClient


def remove_download(
    source: str,
    filename: str,
    out_dir: Path,
) -> BooruUndownloadResult:
    """取消选中时删除已下载的图、旁路 txt 与 meta.

    以完整文件名(如 ``d12345.png``)为参数, 由文件名反推 stem 与 pid, 无需探测.

    Args:
        source (str): 来源名.
        filename (str): 完整文件名 (如 ``d12345.png``).
        out_dir (Path): 输出目录.

    Returns:
        BooruUndownloadResult: 删除结果, 含被删文件列表与图片文件名 (供缩略图清理).

    Callers:
        - ``routes/online_sources.py:undownload``
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


def post_is_video(post: BooruPost) -> bool:
    """判断帖子是否为视频帖, 统一各来源的识别标准.

    优先依据 ``post.file_ext`` (如 danbooru 提供), 其次依据 ``post.file_url``
    的后缀 (如 gelbooru 不提供 file_ext), 两者任一击中 ``VIDEO_EXTS`` 即视为
    视频帖。

    Args:
        post: 帖子.

    Returns:
        bool: 是否为视频帖.

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource.normalize``
        - ``services/online_sources/gelbooru/source.py:GelbooruSource.normalize``
    """
    if post.file_ext and ("." + post.file_ext.lower()) in VIDEO_EXTS:
        return True
    if post.file_url:
        return Path(urlparse(post.file_url).path).suffix.lower() in VIDEO_EXTS
    return False


async def download_raw_file(post: BooruPost, out_dir: Path) -> dict:
    """下载视频帖原始文件到目标目录, 返回供 ``download_post`` 包装的结果词典.

    优先取 ``post.file_url``, 回退 ``large_file_url``; 以视频类型 Accept 头
    拉取原始字节并写入 ``out_dir / post.filename``, 不做图片校验与格式转换。
    各来源 ``download`` 的视频帖分支统一复用本函数, 保证视频写入方式一致。

    Args:
        post: 视频帖 (``is_video`` 为真).
        out_dir: 输出目录 (数据集 ``video``).

    Returns:
        dict: 含 ``path``/``stem``/``id``, 供 ``download_post`` 包装.

    Raises:
        ValueError: 无可用 file_url, 或下载返回非 200 / 空内容.

    Callers:
        - ``services/online_sources/danbooru/source.py:DanbooruSource.download``
        - ``services/online_sources/gelbooru/source.py:GelbooruSource.download``
    """
    urls = [u for u in (post.file_url, post.large_file_url) if u]
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise ValueError("该帖无 file_url(可能需登录或已删除)")
    last_err = "无法下载视频"
    for url in urls:
        async with HttpClient() as client:
            resp = await client.get(
                url,
                headers={"accept": "video/webm,video/mp4,video/*,*/*;q=0.8"},
                timeout=60.0,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                last_err = f"下载 HTTP {resp.status_code}"
                continue
            if not resp.content:
                last_err = "下载到空内容"
                continue
            video_path = out_dir / post.filename
            await asyncio.to_thread(video_path.write_bytes, resp.content)
            return {"path": str(video_path), "stem": video_path.stem, "id": post.id}
    raise ValueError(last_err)


async def download_post(
    source: str,
    post: BooruPost,
    out_dir: Path,
    *,
    overwrite: bool = False,
    anima_format: bool = True,
    include_meta_tags: bool = False,
    keep_underscores: bool = False,
    emit: ProgressFn | None = None,
) -> BooruDownloadResult:
    """下载帖子到目标目录, 并写旁路标签与 meta.

    全部委托给对应 ``SourceClient.download`` 实现 (图片帖经 ``out_dir``
    筛选子目录, 视频帖经 ``out_dir`` ``video`` 目录), 本层只做前缀校验与
    结果翻译。``emit`` (仅视频帖有意义) 原样透传给 ``SourceClient.download``。

    Args:
        source: 来源名。
        post: 前端搜索阶段已持有的帖子 (含原图 URL 与标签)。
        out_dir: 输出目录。
        overwrite: 是否覆盖已下载文件。
        anima_format: 按 Anima 格式输出标签。
        include_meta_tags: 是否保留元数据标签。
        keep_underscores: 是否保留下划线。
        emit: 下载进度回调, 形如 ``(pct, total)``.

    Returns:
        BooruDownloadResult: 下载结果。

    Callers:
        - ``routes/online_sources.py:download``
        - ``services/online_sources/batch.py:download_one``
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
        emit=emit,
    )
    return BooruDownloadResult(source=source, **result)


def local_rel_path(root: Path, abs_path: str | Path) -> str:
    """绝对路径转数据集相对路径.

    Args:
        root (Path): 数据集根目录.
        abs_path (str | Path): 绝对路径.

    Returns:
        str: 数据集相对路径, 分隔符统一为 ``/``.

    Callers:
        - ``routes/online_sources.py:download``
        - ``services/online_sources/batch.py:download_one``
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

        for meta_file in sorted(meta_dir.glob("*.json"), key=lambda p: natural_key(p.name)):
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
