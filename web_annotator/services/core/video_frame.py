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
import threading
from pathlib import Path

import numpy as np

from services.core.video_scanner import cover_hash
from services.core.work_dir import resolve_under
from services.models.video import BitstreamTypes

# 位流缓存目录名(数据集根下)
CACHE_DIRNAME = ".video_cache"
# 保存帧文件名模板
FRAME_NAME_TPL = "{stem}_frame_{idx:06d}.png"
# 包大小量化档位: 字节 / 1024 = KB
_SIZE_KB = 1024
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

    def load_bitstream(self, video_rel: str) -> BitstreamTypes:
        """读取已持久化的紧凑帧位流; 无则返回空位流.

        数据以紧凑 ``{"t", "s"}`` 存储(``s`` 已量化到 KB), 读取即得可直接返回的
        位流; 若发现旧版逐帧 ``{str(idx): {"type","size"}}`` 格式(包大小为字节),
        则一次性迁移为紧凑格式写入磁盘并返回, 使已分析视频下一次读取同样受益。

        Args:
            video_rel: 相对数据集根的视频路径.

        Returns:
            BitstreamTypes: 紧凑位流; 无缓存或损坏时为空位流.

        Callers:
            - `VideoFrameStore.analyze_bitstream`
            - `routes.video:video_detail`
        """
        path = self._bitstream_path(video_rel)
        if not path.is_file():
            return BitstreamTypes()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return BitstreamTypes()
        if isinstance(data, dict) and isinstance(data.get("t"), str) and isinstance(data.get("s"), list):
            return BitstreamTypes(t=data["t"], s=[max(0, int(x)) for x in data["s"]])
        bits = self._legacy_to_compact(data)
        if bits.t:
            self.save_bitstream(video_rel, bits)
        return bits

    @staticmethod
    def _legacy_to_compact(data: object) -> BitstreamTypes:
        """把旧版逐帧位流 ``{str(idx): {"type","size"}}`` 转为紧凑格式.

        Args:
            data: 旧版 JSON 反序列化结果.

        Returns:
            BitstreamTypes: 紧凑位流, 包大小由字节量化到 KB.

        Callers:
            - `VideoFrameStore.load_bitstream`
        """
        t_chars: list[str] = []
        sizes: list[int] = []
        if isinstance(data, dict):
            items = [(int(k), v) for k, v in data.items() if str(k).isdigit() and isinstance(v, dict)]
            for _, v in sorted(items):
                type_char = str(v.get("type", "?"))[:1] or "?"
                t_chars.append(type_char)
                sizes.append(max(0, int(v.get("size", 0) or 0)) // _SIZE_KB)
        return BitstreamTypes(t="".join(t_chars), s=sizes)

    def save_bitstream(self, video_rel: str, bits: BitstreamTypes) -> None:
        """持久化紧凑帧位流(``s`` 各元素为量化到 KB 的大小).

        Args:
            video_rel: 相对数据集根的视频路径.
            bits: 待持久化的紧凑位流.

        Callers:
            - `VideoFrameStore.load_bitstream`
            - `VideoFrameStore.analyze_bitstream`
        """
        d = self._cache_dir(video_rel)
        d.mkdir(parents=True, exist_ok=True)
        tmp = self._bitstream_path(video_rel).with_suffix(".tmp")
        tmp.write_text(json.dumps(bits.model_dump()), encoding="utf-8")
        tmp.replace(self._bitstream_path(video_rel))

    # ---- 帧读取与编码 ----

    def _video_path(self, video_rel: str) -> Path:
        """将相对路径解析为数据集内绝对路径.

        复用 ``resolve_under`` 做词法规范化 (消除 ``..``/``.`` 但不跟随符号链接),
        因此数据集内指向外部存储的视频链接 (如 ``video -> /mnt/...``) 也能正常
        解析, 与 ``routes.deps.resolve_path`` 的越界判定标准一致。

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

    def frame_bytes(
        self,
        video_rel: str,
        index: int,
        *,
        fmt: str = "jpg",
        max_size: int | None = None,
    ) -> bytes:
        """读取一帧, 可选等比缩略后编码为 JPEG/PNG 字节.

        抽帧预览分两种消费: 主图需清晰, 缩略条仅需小尺寸定位。通过
        ``max_size`` 限制最长边以减小传输体积, 缩小用 INTER_AREA 保证质量;
        None 时返回原始分辨率帧。

        Args:
            video_rel: 相对数据集根的视频路径.
            index: 帧索引.
            fmt: 编码格式("jpg" 或 "png").
            max_size: 缩略后最长边像素数; None 表示不缩略.

        Returns:
            bytes: 编码后图像数据.

        Raises:
            VideoFrameError: 帧读取失败或编码失败.

        Callers:
            - `routes/video.py:video_frame`
        """
        frame = self.extract_frame(video_rel, index)
        if max_size:
            h, w = frame.shape[:2]
            longest = max(w, h)
            if longest > max_size:
                scale = max_size / longest
                frame = cv2.resize(
                    frame,
                    (int(w * scale), int(h * scale)),
                    interpolation=cv2.INTER_AREA,
                )
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
    ) -> BitstreamTypes:
        """用 ffprobe 提取每帧的编码类型(I/P/B)与包大小, 已分析的帧跳过.

        一次性运行 ffprobe 输出全部帧的 ``pict_type`` 与 ``pkt_size``; 若缓存已
        存在则直接复用, 不重复扫描。包大小在持久化前量化到 KB, 与紧凑存储格式一致。

        Args:
            video_rel: 相对数据集根的视频路径.
            total: 视频总帧数(仅用于进度与校验).
            cancel_fn: 可选取消回调, 返回真值即停止.
            emit: 可选进度回调 ``(done, total)``.

        Returns:
            BitstreamTypes: 紧凑位流(``s`` 为量化到 KB 的大小).

        Raises:
            VideoFrameError: 视频无法打开或 ffprobe 失败.

        Callers:
            - `routes/video.py:start_analysis`
        """
        path = self._video_path(video_rel)
        cached = self.load_bitstream(video_rel)
        if cached.t:
            if emit and total:
                emit(total, total)
            return cached

        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "frame=pict_type,pkt_size",
            "-of", "csv=p=0",
            str(path),
        ]
        try:
            proc: subprocess.Popen[bytes] = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as e:
            raise VideoFrameError(f"位流分析失败: {e}") from e

        err_lines: list[str] = []

        def drain_stderr() -> None:
            """后台线程收集 ffprobe stderr 输出(``-v error`` 下仅错误信息).

            Callers:
                - `VideoFrameStore.analyze_bitstream`
            """
            for raw in proc.stderr or ():
                err_lines.append(raw.decode("utf-8", "replace").strip())

        err_thread = threading.Thread(target=drain_stderr, daemon=True)
        err_thread.start()

        t_chars: list[str] = []
        sizes: list[int] = []
        cancelled = False
        try:
            for raw in getattr(proc, "stdout") or ():
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                if cancel_fn and cancel_fn():
                    cancelled = True
                    break
                # ffprobe `frame=pict_type,pkt_size -of csv=p=0` 输出字段顺序为
                # pkt_size,pict_type, 需按此顺序解析
                t_chars.append((parts[1] or "?")[:1])
                sizes.append((int(parts[0]) if parts[0] else 0) // _SIZE_KB)
                # ffprobe 逐帧流式输出, 每 50 帧回报一次进度
                if emit and total and len(sizes) % 50 == 0:
                    emit(len(sizes), max(total, len(sizes)))
            if cancelled:
                # 取消时终止 ffprobe, 否则 stdout 管道写满后 wait 会空等至超时
                proc.terminate()
            proc.wait(timeout=_FFPROBE_TIMEOUT)
        finally:
            err_thread.join()
        if cancelled:
            # 取消不持久化部分缓存, 以免下次复用残缺位流
            raise VideoFrameError("位流分析已取消")
        if proc.returncode != 0:
            detail = " ".join(err_lines).strip() or str(proc.returncode)
            raise VideoFrameError(f"位流分析失败: {detail}")
        if emit and total:
            emit(max(total, len(sizes)), total)
        bits = BitstreamTypes(t="".join(t_chars), s=sizes)
        self.save_bitstream(video_rel, bits)
        return bits


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