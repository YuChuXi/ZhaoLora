"""视频抽帧与位流帧类型分析服务.

抽帧用 OpenCV 按帧索引精确定位; 帧类型分析用 ffprobe 提取每帧的
编码类型(I/P/B 帧)与包大小, 写入数据集 ``.video_cache/<hash>/bitstream.json``
持久化, 刷新后仍可复用。保存帧写入 ``out_dir``(通常是数据集 ``main``), 命名
``{video_stem}_frame_{index:06d}.png``。
"""

from __future__ import annotations

import cv2
import json
import subprocess
from pathlib import Path

import numpy as np

from services.core.video_scanner import cover_hash
from services.core.work_dir import resolve_under
from services.models.video import FrameType

# 位流缓存目录名(数据集根下)
CACHE_DIRNAME = ".video_cache"
# 保存帧文件名模板
FRAME_NAME_TPL = "{stem}_frame_{idx:06d}.png"
# ffprobe 单次分析的超时上限(秒), 大视频较长
_FFPROBE_TIMEOUT = 300


class VideoFrameError(RuntimeError):
    """视频无法解码或指定帧不存在.

    Callers:
        - `routes/video.py`
    """


class VideoFrameStore:
    """视频帧读取 / 保存 / 删除与位流帧类型分析.

    Callers:
        - `routes/video.py`
    """

    def __init__(self, data_dir: Path, out_dir: Path) -> None:
        """绑定数据集根与保存帧的输出目录.

        Args:
            data_dir: 数据集根目录(用于视频定位与位流缓存).
            out_dir: 保存帧的输出目录(数据集 ``main``).

        Callers:
            - `routes/video.py:video_detail`
        """
        self.data_dir = data_dir
        self.out_dir = out_dir
        self._cache_root = data_dir / CACHE_DIRNAME

    # ---- 路径与缓存 ----

    def _cache_dir(self, video_rel: str) -> Path:
        """位流缓存目录. 引用: ``load_bitstream`` / ``save_bitstream``. """
        return self._cache_root / cover_hash(video_rel)

    def _bitstream_path(self, video_rel: str) -> Path:
        """位流缓存文件路径. 引用: ``load_bitstream`` / ``save_bitstream``. """
        return self._cache_dir(video_rel) / "bitstream.json"

    def load_bitstream(self, video_rel: str) -> dict[int, FrameType]:
        """读取已持久化的帧类型位流; 无则返回空字典.

        Callers:
            - `VideoFrameStore.analyze_bitstream`
        """
        path = self._bitstream_path(video_rel)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        result: dict[int, FrameType] = {}
        for k, v in data.items():
            if str(k).isdigit() and isinstance(v, dict):
                result[int(k)] = FrameType(
                    type=str(v.get("type", "?")),
                    size=int(v.get("size", 0) or 0),
                )
        return result

    def save_bitstream(self, video_rel: str, bitstream: dict[int, FrameType]) -> None:
        """持久化帧类型位流. 引用: ``VideoFrameStore.analyze_bitstream``. """
        d = self._cache_dir(video_rel)
        d.mkdir(parents=True, exist_ok=True)
        tmp = self._bitstream_path(video_rel).with_suffix(".tmp")
        payload = {str(k): v.model_dump() for k, v in bitstream.items()}
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._bitstream_path(video_rel))

    # ---- 帧读取与编码 ----

    def _video_path(self, video_rel: str) -> Path:
        """将相对路径解析为数据集内绝对路径.

        复用 ``resolve_under`` 做词法规范化 (消除 ``..``/``.`` 但不跟随符号链接),
        因此数据集内指向外部存储的视频链接 (如 ``video -> /mnt/...``) 也能正常
        解析, 与 ``routes.deps.resolve_path`` 的越界判定口径一致。

        Args:
            video_rel: 相对数据集根的视频路径.

        Returns:
            Path: 数据集内绝对路径.

        Raises:
            VideoFrameError: 路径为空或越界.

        Callers:
            - `VideoFrameStore.extract_frame`
            - `VideoFrameStore.analyze_bitstream`
        """
        try:
            return resolve_under(self.data_dir, video_rel)
        except ValueError as e:
            raise VideoFrameError(str(e)) from e

    def extract_frame(self, video_rel: str, index: int) -> np.ndarray:
        """按帧索引读取一帧 BGR 图像.

        Args:
            video_rel: 相对数据集根的视频路径.
            index: 帧索引.

        Returns:
            np.ndarray: 单帧 BGR 图像.

        Raises:
            VideoFrameError: 视频无法打开或该帧不存在.

        Callers:
            - `VideoFrameStore.frame_bytes`
            - `VideoFrameStore.save_frame`
        """
        path = self._video_path(video_rel)
        cap = cv2.VideoCapture(str(path))
        try:
            if not cap.isOpened():
                raise VideoFrameError("无法打开视频")
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(index))
            ok, frame = cap.read()
            if not ok or frame is None:
                raise VideoFrameError(f"帧 {index} 不存在")
            return frame
        finally:
            cap.release()

    def frame_bytes(self, video_rel: str, index: int, *, fmt: str = "jpg") -> bytes:
        """读取一帧并编码为 JPEG/PNG 字节.

        Args:
            video_rel: 相对数据集根的视频路径.
            index: 帧索引.
            fmt: 编码格式("jpg" 或 "png").

        Returns:
            bytes: 编码后图像数据.

        Raises:
            VideoFrameError: 帧读取失败.

        Callers:
            - `routes/video.py:video_frame`
        """
        frame = self.extract_frame(video_rel, index)
        ext = ".png" if fmt == "png" else ".jpg"
        ok, buf = cv2.imencode(ext, frame)
        if not ok:
            raise VideoFrameError("帧编码失败")
        return buf.tobytes()

    # ---- 保存帧 ----

    def frame_name(self, stem: str, index: int) -> str:
        """拼接保存帧文件名. 引用: ``save_frame`` / ``remove_frame`` / ``saved_indices``. """
        return FRAME_NAME_TPL.format(stem=stem, idx=index)

    def save_frame(self, video_rel: str, index: int) -> Path:
        """把指定帧保存为 PNG 到输出目录.

        Args:
            video_rel: 相对数据集根的视频路径.
            index: 帧索引.

        Returns:
            Path: 已保存的 PNG 绝对路径.

        Raises:
            VideoFrameError: 帧读取失败.

        Callers:
            - `routes/video.py:save_frame`
        """
        frame = self.extract_frame(video_rel, index)
        stem = Path(video_rel).stem
        dest = self.out_dir / self.frame_name(stem, index)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            raise VideoFrameError("帧编码失败")
        tmp = dest.with_suffix(".tmp.png")
        tmp.write_bytes(buf.tobytes())
        tmp.replace(dest)
        return dest

    def remove_frame(self, video_rel: str, index: int) -> bool:
        """删除已保存的指定帧 PNG.

        Args:
            video_rel: 相对数据集根的视频路径.
            index: 帧索引.

        Returns:
            bool: 是否删除了文件.

        Callers:
            - `routes/video.py:delete_frame`
        """
        stem = Path(video_rel).stem
        dest = self.out_dir / self.frame_name(stem, index)
        if dest.is_file():
            dest.unlink()
            return True
        return False

    def saved_indices(self, video_rel: str) -> list[int]:
        """扫描输出目录, 返回该视频已保存的帧索引(升序).

        Callers:
            - `routes/video.py:video_detail`
        """
        stem = Path(video_rel).stem
        prefix = f"{stem}_frame_"
        indices: list[int] = []
        if not self.out_dir.is_dir():
            return indices
        for f in self.out_dir.iterdir():
            if not f.is_file() or f.suffix.lower() != ".png":
                continue
            if not f.stem.startswith(prefix):
                continue
            num = f.stem[len(prefix):]
            if num.isdigit():
                indices.append(int(num))
        return sorted(indices)

    # ---- 位流帧类型分析 ----

    def analyze_bitstream(
        self,
        video_rel: str,
        total: int,
        cancel_fn=None,
        emit=None,
    ) -> dict[int, FrameType]:
        """用 ffprobe 提取每帧的编码类型(I/P/B)与包大小, 已分析的帧跳过.

        一次性运行 ffprobe 输出全部帧的 ``pict_type`` 与 ``pkt_size``; 若缓存已
        存在则直接复用, 不重复扫描。

        Args:
            video_rel: 相对数据集根的视频路径.
            total: 视频总帧数(仅用于进度与校验).
            cancel_fn: 可选取消回调, 返回真值即停止.
            emit: 可选进度回调 ``(done, total)``.

        Returns:
            dict[int, dict]: 帧索引 -> ``{"type": I/P/B, "size": int}``.

        Raises:
            VideoFrameError: 视频无法打开或 ffprobe 失败.

        Callers:
            - `routes/video.py:start_analysis`
        """
        path = self._video_path(video_rel)
        cached = self.load_bitstream(video_rel)
        if cached:
            if emit and total:
                emit(total, total)
            return cached

        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "frame=pict_type,pkt_size",
            "-of", "json", str(path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise VideoFrameError(f"位流分析失败: {e}") from e
        if res.returncode != 0 or not res.stdout.strip():
            raise VideoFrameError(f"位流分析失败: {res.stderr.strip() or res.returncode}")

        bitstream: dict[int, FrameType] = {}
        try:
            frames = (json.loads(res.stdout) or {}).get("frames") or []
        except ValueError as e:
            raise VideoFrameError(f"位流分析输出解析失败: {e}") from e
        for i, f in enumerate(frames):
            if cancel_fn and cancel_fn():
                break
            bitstream[i] = FrameType(
                type=str(f.get("pict_type", "?")),
                size=int(f.get("pkt_size", 0) or 0),
            )
            if emit and (i + 1) % 50 == 0:
                emit(i + 1, max(total, len(frames)))
        if emit and total:
            emit(max(total, len(frames)), total)
        self.save_bitstream(video_rel, bitstream)
        return bitstream


def total_frames(video_rel: str, data_dir: Path) -> int:
    """读取视频总帧数(fps × 时长). 引用: ``routes/video.py``. """
    path = (data_dir / video_rel.lstrip("/")).resolve()
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return 0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        return max(0, n)
    finally:
        cap.release()