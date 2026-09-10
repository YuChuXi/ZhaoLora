"""视频扫描器: 在数据集 ``video`` 目录中发现视频并生成封面.

约束:
  - 目录内扁平扫描(不递归), 与图片扫描一致
  - 识别 ``VIDEO_EXTS``
  - 封面由 PyAV 抽取视频中间帧生成, 缓存到数据集根 ``.video_covers/``
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import av
import cv2

from services.core.atomic import atomic_write_bytes
from services.core.natural import natural_key
from services.models import VideoInfo

logger = logging.getLogger(__name__)

# 封面缓存目录名(数据集根下)
COVER_DIRNAME = ".video_covers"
# 封面长边像素
COVER_MAX_SIDE = 480
# 封面 JPEG 编码质量(0-100)
COVER_JPEG_QUALITY = 90

VIDEO_EXTS = frozenset(
    {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts"}
)


class VideoScanError(RuntimeError):
    """视频探测或封面生成失败.

    Callers:
        - ``services/core/video_scanner.py:scan_videos``
        - ``routes/video.py:``
    """


def _probe(path: Path) -> tuple[int, int, float, int]:
    """用 PyAV 读取视频宽高、时长与总帧数.

    总帧数由 ``时长 × 平均帧率`` 估算: FLV 等容器没有帧数元数据, PyAV 亦不
    暴露 ``nb_frames``, 该估算对 OBS 录播等恒定帧率视频精确。

    Args:
        path: 视频绝对路径.

    Returns:
        ``(width, height, duration, total_frames)``; 无法读取时全为 0.

    Callers:
        - ``services/core/video_scanner.py:probe_video``
        - ``services/core/video_scanner.py:ensure_cover``
    """
    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                return 0, 0, 0.0, 0
            stream = container.streams.video[0]
            width = stream.width or 0
            height = stream.height or 0
            duration = (container.duration or 0) / av.time_base
            rate = stream.average_rate
            total = round(duration * float(rate)) if (duration > 0 and rate and rate > 0) else 0
        return width, height, duration, total
    except av.FFmpegError:
        return 0, 0, 0.0, 0


def probe_video(path: Path) -> tuple[int, int, float, int]:
    """读取视频宽高、时长与总帧数(绝对路径).

    Args:
        path: 视频绝对路径.

    Returns:
        ``(width, height, duration, total_frames)``.

    Callers:
        - ``routes/video.py:video_detail``
        - ``routes/video.py:start_analysis``
    """
    return _probe(path)


def scan_videos(video_dir: Path, *, path_prefix: str = "", probe: bool = False) -> list[VideoInfo]:
    """扫描 ``video_dir`` 下视频并按文件名排序.

    ``probe=False`` 时仅列文件(快); ``probe=True`` 时并行探测宽高/时长。

    Args:
        video_dir: 视频目录绝对路径.
        path_prefix: 非空时加在 ``path`` 前(相对数据集根), 通常为目录名 ``video``.
        probe: 是否并行探测宽高/时长.

    Returns:
        按文件名排序的 ``VideoInfo`` 列表.

    Callers:
        - ``routes/video.py:list_videos``
    """
    prefix = (path_prefix or "").replace("\\", "/").strip("/")
    if not video_dir.is_dir():
        return []
    paths: list[Path] = []
    result: list[VideoInfo] = []
    for p in sorted(video_dir.iterdir(), key=lambda p: natural_key(p.name)):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        rel = f"{prefix}/{p.name}" if prefix else p.name
        paths.append(p)
        result.append(
            VideoInfo(
                path=rel,
                name=p.name,
                stem=p.stem,
                size=p.stat().st_size,
            )
        )
    if not result or not probe:
        return result
    with ThreadPoolExecutor(max_workers=len(result)) as ex:
        futures = {ex.submit(_probe, p): i for i, p in enumerate(paths)}
        for f in as_completed(futures):
            i = futures[f]
            try:
                w, h, dur, _ = f.result()
            except Exception as e:
                logger.error("视频探测失败 %s: %s", paths[i], e)
                continue
            result[i].width = w
            result[i].height = h
            result[i].duration = dur
    return result


def cover_hash(video_rel_path: str, *, max_side: int = COVER_MAX_SIDE) -> str:
    """计算视频封面缓存文件名 hash(计入长边尺寸与视频 mtime).

    Args:
        video_rel_path: 相对数据集根的视频路径.
        max_side: 封面长边像素.

    Returns:
        str: 12 位十六进制 hash 字符串.

    Callers:
        - ``services/core/video_scanner.py:cover_path``
    """
    key = f"{video_rel_path}\0s{max_side}".encode()
    return hashlib.md5(key).hexdigest()[:12]


def cover_path(cover_dir: Path, video_rel_path: str) -> Path:
    """获取视频封面在缓存目录中的绝对路径.

    Args:
        cover_dir: 封面缓存目录(数据集根 ``.video_covers``).
        video_rel_path: 相对数据集根的视频路径.

    Returns:
        Path: 封面文件路径(``.video_covers/<hash>.jpg``).

    Callers:
        - ``services/core/video_scanner.py:ensure_cover``
        - ``routes/video.py:list_videos``
        - ``routes/video.py:video_cover``
    """
    return cover_dir / f"{cover_hash(video_rel_path)}.jpg"


def ensure_cover(video_path: Path, cover_dir: Path, video_rel_path: str) -> Path:
    """确保视频封面存在, 缺失则用 PyAV 抽中间帧生成.

    抽取视频中间时刻的一帧并缩放到 ``COVER_MAX_SIDE`` 长边, 存为 JPEG;
    若视频存在但无法抽取, 抛 ``VideoScanError``。

    Args:
        video_path: 视频绝对路径.
        cover_dir: 封面缓存目录.
        video_rel_path: 相对数据集根的视频路径.

    Returns:
        Path: 封面文件绝对路径.

    Raises:
        VideoScanError: 视频解码或封面编码失败.

    Callers:
        - ``routes/video.py:list_videos``
        - ``routes/video.py:video_cover``
    """
    out = cover_path(cover_dir, video_rel_path)
    if out.is_file():
        return out
    cover_dir.mkdir(parents=True, exist_ok=True)
    _w, _h, dur, _total = _probe(video_path)
    mid = max(0.0, dur / 2.0) if dur > 0 else 0.0
    try:
        with av.open(str(video_path)) as container:
            stream = container.streams.video[0]
            if mid > 0:
                # seek offset 以流 time_base 计(秒 / time_base)
                container.seek(int(mid / float(stream.time_base)), stream=stream, backward=True)
            frame = next(container.decode(stream), None)
    except av.FFmpegError as e:
        raise VideoScanError(f"封面生成失败: {e}") from e
    if frame is None:
        raise VideoScanError(f"封面生成失败: {video_path.name} 无可解码帧")
    img = frame.to_ndarray(format="bgr24")
    h, w = img.shape[:2]
    longest = max(w, h)
    if longest > COVER_MAX_SIDE:
        scale = COVER_MAX_SIDE / longest
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, COVER_JPEG_QUALITY])
    if not ok:
        raise VideoScanError("封面生成失败: JPEG 编码失败")
    atomic_write_bytes(out, buf.tobytes())
    return out
