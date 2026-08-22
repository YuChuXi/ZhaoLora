"""视频扫描器: 在数据集 ``video`` 目录中发现视频并生成封面.

约束:
  - 目录内扁平扫描(不递归), 与图片扫描一致
  - 识别 ``VIDEO_EXTS``
  - 封面由 ffmpeg 抽取视频中间帧生成, 缓存到数据集根 ``.video_covers/``
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _ffmpeg_supports_rawvideo(path: str) -> bool:
    """检测 ffmpeg 是否具备 rawvideo demuxer(窗口帧编码的前提).

    受限静态构建(如随 IDE 分发的 ffmpeg)可能缺少 rawvideo demuxer, 无法从原始帧
    编码; 返回 True 表示可正常用于 ``_encode_h264_annexb``。

    Args:
        path: ffmpeg 可执行路径.

    Returns:
        bool: 是否支持 rawvideo demuxer.

    Callers:
        - `video_scanner.resolve_ffmpeg`
    """
    try:
        out = subprocess.run(
            [path, "-hide_banner", "-demuxers"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "rawvideo" in out.stdout


@functools.lru_cache(maxsize=1)
def resolve_ffmpeg() -> str:
    """解析一个支持 rawvideo demuxer 的 ffmpeg 可执行路径.

    PATH 中受限静态构建可能优先于系统完整版, 故遍历 PATH 候选并补充常见完整版
    路径, 返回首个支持 rawvideo 的; 均不支持时抛出 ``VideoScanError``。

    Returns:
        str: ffmpeg 可执行路径.

    Raises:
        VideoScanError: 未找到支持 rawvideo 的 ffmpeg.

    Callers:
        - 模块级常量 ``FFMPEG`` / ``FFPROBE`` 的初始化
    """
    seen: set[str] = set()
    candidates: list[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        p = os.path.join(d, "ffmpeg")
        rp = os.path.realpath(p)
        if os.path.isfile(p) and rp not in seen:
            seen.add(rp)
            candidates.append(p)
    for extra in ("/usr/bin/ffmpeg", "/bin/ffmpeg"):
        if extra not in seen and os.path.isfile(extra):
            candidates.append(extra)
    for c in candidates:
        if _ffmpeg_supports_rawvideo(c):
            return c
    raise VideoScanError("未找到支持 rawvideo 的 ffmpeg")


# 集中解析的 ffmpeg/ffprobe 路径, 供视频扫描与抽帧复用, 避免 PATH 中受限构建干扰
FFMPEG = resolve_ffmpeg()
FFPROBE = os.path.join(os.path.dirname(FFMPEG), "ffprobe")


def _probe(path: Path) -> tuple[int, int, float, int]:
    """用 ffprobe 读取视频宽高、时长与总帧数.

    帧数优先取 ``nb_frames`` 元数据(MP4/MKV 可靠); 缺失时(FLV 等)用
    ``duration × r_frame_rate`` 估算。两种方式均比 OpenCV 的
    ``CAP_PROP_FRAME_COUNT`` 更可靠(后者对 FLV 常偏高)。

    Args:
        path: 视频绝对路径.

    Returns:
        ``(width, height, duration, total_frames)``; 无法读取时全为 0.

    Callers:
        - `video_scanner.probe_video`
    """
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,nb_frames,r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0, 0.0, 0
    if out.returncode != 0 or not out.stdout.strip():
        return 0, 0, 0.0, 0
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return 0, 0, 0.0, 0
    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    width = int(stream.get("width", 0) or 0)
    height = int(stream.get("height", 0) or 0)
    duration = 0.0
    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    # 帧数: 优先 nb_frames 元数据; 缺失则用 duration × fps 估算
    total = 0
    nb = stream.get("nb_frames")
    if nb and str(nb).isdigit():
        total = int(nb)
    elif duration > 0:
        rfr = stream.get("r_frame_rate", "0/1")
        try:
            num, den = rfr.split("/")
            fps = float(num) / float(den) if float(den) else 0.0
            if fps > 0:
                total = round(duration * fps)
        except (ValueError, ZeroDivisionError):
            pass
    return width, height, duration, total


def probe_video(path: Path) -> tuple[int, int, float, int]:
    """读取视频宽高、时长与总帧数(绝对路径).

    Args:
        path: 视频绝对路径.

    Returns:
        ``(width, height, duration, total_frames)``.

    Callers:
        - `routes/video.py:video_detail`
        - `routes/video.py:start_analysis`
    """
    return _probe(path)


def scan_videos(video_dir: Path, *, path_prefix: str = "", probe: bool = False) -> list[VideoInfo]:
    """扫描 ``video_dir`` 下视频并按文件名排序.

    ``probe=False`` 时仅列文件(快); ``probe=True`` 时并行 ffprobe 探测宽高/时长。

    Args:
        video_dir: 视频目录绝对路径.
        path_prefix: 非空时加在 ``path`` 前(相对数据集根), 通常为目录名 ``video``.
        probe: 是否并行 ffprobe 探测宽高/时长.

    Returns:
        按文件名排序的 ``VideoInfo`` 列表.

    Callers:
        - `routes/video.py:list_videos`
    """
    prefix = (path_prefix or "").replace("\\", "/").strip("/")
    if not video_dir.is_dir():
        return []
    paths: list[Path] = []
    result: list[VideoInfo] = []
    for p in sorted(video_dir.iterdir()):
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
                print(f"视频探测失败 {paths[i]}: {e}", file=sys.stderr)
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
    w, h, dur, _total = _probe(video_path)
    mid = max(0.0, dur / 2.0) if dur > 0 else 0.0
    vm = f"scale={COVER_MAX_SIDE}:-2" if w else "scale=-2:240"
    cmd = [
        FFMPEG, "-y", "-v", "error",
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