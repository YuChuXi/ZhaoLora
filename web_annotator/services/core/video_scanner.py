"""视频扫描器: 在数据集 ``video`` 目录中发现视频并生成封面.

约束:
  - 目录内扁平扫描(不递归), 与图片扫描一致
  - 识别 ``VIDEO_EXTS``
  - 封面由 ffmpeg 抽取视频中间帧生成, 缓存到数据集根 ``.video_covers/``
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from services.models import VideoInfo

# 视频目录名(数据集根下)
VIDEO_DIRNAME = "video"
# 封面缓存目录名(数据集根下)
COVER_DIRNAME = ".video_covers"
# 封面长边像素
COVER_MAX_SIDE = 480

VIDEO_EXTS = frozenset(
    {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts"}
)


class VideoScanError(RuntimeError):
    """视频探测或封面生成失败.

    Callers:
        - `video_scanner.scan_videos`
        - `routes/video.py`
    """


def _probe(path: Path) -> tuple[int, int, float]:
    """用 ffprobe 读取视频宽高与时长的纯逻辑.

    Args:
        path: 视频绝对路径.

    Returns:
        ``(width, height, duration)``; 无法读取时宽高为 0, 时长为 0.0.

    Callers:
        - `video_scanner.probe_video`
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0, 0.0
    if out.returncode != 0 or not out.stdout.strip():
        return 0, 0, 0.0
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return 0, 0, 0.0
    streams = data.get("streams") or []
    width = int(streams[0].get("width", 0) or 0) if streams else 0
    height = int(streams[0].get("height", 0) or 0) if streams else 0
    duration = 0.0
    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return width, height, duration


def probe_video(path: Path) -> tuple[int, int, float]:
    """读取视频宽高与时长(绝对路径).

    Args:
        path: 视频绝对路径.

    Returns:
        ``(width, height, duration)``.

    Callers:
        - `routes/video.py:list_videos`
        - `routes/video.py:video_detail`
    """
    return _probe(path)


def scan_videos(video_dir: Path, *, path_prefix: str = "") -> list[VideoInfo]:
    """扫描 ``video_dir`` 下视频并按文件名排序.

    Args:
        video_dir: 视频目录绝对路径.
        path_prefix: 非空时加在 ``path`` 前(相对数据集根), 通常为目录名 ``video``.

    Returns:
        按文件名排序的 ``VideoInfo`` 列表.

    Callers:
        - `routes/video.py:list_videos`
    """
    prefix = (path_prefix or "").replace("\\", "/").strip("/")
    if not video_dir.is_dir():
        return []
    result: list[VideoInfo] = []
    for p in sorted(video_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        rel = f"{prefix}/{p.name}" if prefix else p.name
        w, h, dur = _probe(p)
        result.append(
            VideoInfo(
                path=rel,
                name=p.name,
                stem=p.stem,
                width=w,
                height=h,
                duration=dur,
                size=p.stat().st_size,
            )
        )
    return result


def cover_hash(video_rel_path: str, *, max_side: int = COVER_MAX_SIDE) -> str:
    """计算视频封面缓存文件名 hash(计入长边尺寸与视频 mtime).

    Args:
        video_rel_path: 相对数据集根的视频路径.
        max_side: 封面长边像素.

    Returns:
        str: 12 位十六进制 hash 字符串.

    Callers:
        - `video_scanner.cover_path`
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
        - `video_scanner.ensure_cover`
        - `routes/video.py:list_videos`
        - `routes/video.py:video_cover`
    """
    return cover_dir / f"{cover_hash(video_rel_path)}.jpg"


def ensure_cover(video_path: Path, cover_dir: Path, video_rel_path: str) -> Path:
    """确保视频封面存在, 缺失则用 ffmpeg 抽中间帧生成.

    抽取视频中间时刻的一帧并缩放到 ``COVER_MAX_SIDE`` 长边, 存为 JPEG;
    若视频存在但无法抽取, 抛 ``VideoScanError``。

    Args:
        video_path: 视频绝对路径.
        cover_dir: 封面缓存目录.
        video_rel_path: 相对数据集根的视频路径.

    Returns:
        Path: 封面文件绝对路径.

    Raises:
        VideoScanError: ffmpeg 抽取失败.

    Callers:
        - `routes/video.py:list_videos`
        - `routes/video.py:video_cover`
    """
    out = cover_path(cover_dir, video_rel_path)
    if out.is_file():
        return out
    cover_dir.mkdir(parents=True, exist_ok=True)
    w, h, dur = _probe(video_path)
    mid = max(0.0, dur / 2.0) if dur > 0 else 0.0
    vm = f"scale={COVER_MAX_SIDE}:-2" if w else "scale=-2:240"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{mid:g}", "-i", str(video_path),
        "-frames:v", "1", "-vf", vm, "-q:v", "3",
        str(out),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VideoScanError(f"封面生成失败: {e}") from e
    if res.returncode != 0 or not out.is_file():
        raise VideoScanError(f"封面生成失败: {res.stderr.strip() or res.returncode}")
    return out