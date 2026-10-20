"""视频抽帧与位流帧类型分析服务.

全部视频操作经 PyAV(FFmpeg 库绑定)在进程内完成: 抽帧按帧号定位、帧类型分析
逐包解码提取 ``pict_type`` 与包大小、帧窗口段直接流复制(不经重编码)并经
``h264_mp4toannexb`` 位流滤波器输出 Annex-B 裸流。帧类型分析结果写入数据集
``.video_cache/<hash>/bitstream.json`` 持久化, 刷新后仍可复用。保存帧写入
``out_dir``(通常是数据集 ``main``), 命名 ``{video_stem}_frame_{index:06d}.png``。

帧号语义: 全模块统一为显示顺序帧号, 由 pts 几何换算
``frame_no = round((pts - min_pts) / frame_dur)``, 其中 ``min_pts`` 为首帧 pts,
``frame_dur = 1 / average_rate``(以流 time_base 计)。恒定帧率视频(OBS 录播等)
的 frame.pts 严格等差, 换算结果与解码输出序一致且无累积误差。
"""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np
from av.bitstream import BitStreamFilterContext
from av.codec.context import ThreadType
from av.video.frame import PictureType

from services.core.atomic import atomic_write_bytes, atomic_write_text
from services.core.video_scanner import cover_hash
from services.dataset.workdir import resolve_under
from services.models.video import BitstreamTypes

# 位流缓存目录名(数据集根下)
CACHE_DIRNAME = ".video_cache"
# 保存帧文件名模板
FRAME_NAME_TPL = "{stem}_frame_{idx:06d}.png"
# 包大小量化档位: 字节 / 1024 = KB
_SIZE_KB = 1024
# 解码线程模式: FRAME 与 SLICE 组合, 单 slice 流也能并行解码
_THREAD_TYPE = ThreadType.FRAME | ThreadType.SLICE
# 解码 AVOption: 跳过环滤波。帧类型在 slice 头解析阶段即已确定、抽帧与帧号
# 换算不依赖滤波结果, 跳过环滤波可将解码速度提升约 17%。
_DECODE_OPTIONS = {"skip_loop_filter": "all"}
# B 帧显示重排深度上限(H.264 规范约束)。流复制段的末尾须多复制这些帧,
# 保证窗口内最后帧(B 帧)的后向参考包完整, 否则前端解码会丢帧并使输出序号错位。
_B_REORDER_MARGIN = 16
# ``pict_type`` 枚举到帧类型字符的映射(其余类型一律记 "?")
_PICT_TYPE_CHARS = {
    PictureType.I: "I",
    PictureType.P: "P",
    PictureType.B: "B",
}


class VideoFrameError(RuntimeError):
    """视频无法解码或指定帧不存在.

    Callers:
        - ``routes/video.py:``
    """


@dataclasses.dataclass(frozen=True)
class _StreamLayout:
    """视频流帧号几何: 帧号 = round((pts - min_pts) / frame_dur).

    Callers:
        - ``services/core/video_frame.py:VideoFrameStore._stream_layout``
    """

    min_pts: int          # 首帧 pts(流 time_base 计)
    frame_dur: Fraction   # 帧间隔(流 time_base 计), 即 1 / average_rate


def _frame_no(pts: int, layout: _StreamLayout) -> int:
    """把包/帧的 pts 换算为显示顺序帧号.

    Args:
        pts: 包或帧的 pts(流 time_base 计).
        layout: 流帧号几何.

    Returns:
        int: 显示顺序帧号(0 基).

    Callers:
        - ``services/core/video_frame.py:VideoFrameStore.extract_frame``
        - ``services/core/video_frame.py:VideoFrameStore.segment_bytes``
    """
    return round((pts - layout.min_pts) / layout.frame_dur)


class VideoFrameStore:
    """视频帧读取 / 保存 / 删除与位流帧类型分析.

    Callers:
        - ``routes/video.py:``
    """

    def __init__(self, data_dir: Path, out_dir: Path) -> None:
        """绑定数据集根与保存帧的输出目录.

        Args:
            data_dir: 数据集根目录(用于视频定位与位流缓存).
            out_dir: 保存帧的输出目录(数据集 ``main``).

        Returns:
            None

        Callers:
            - ``routes/video.py:video_detail``
        """
        self.data_dir = data_dir
        self.out_dir = out_dir
        self._cache_root = data_dir / CACHE_DIRNAME
        # 流帧号几何缓存: (路径, mtime, size) -> _StreamLayout, 避免每次请求重扫首包
        self._layouts: dict[tuple, _StreamLayout] = {}

    # ---- 路径与缓存 ----

    def _cache_dir(self, video_rel: str) -> Path:
        """位流缓存目录.

        Args:
            video_rel: 相对数据集根的视频路径.

        Returns:
            Path: 位流缓存目录.
        Callers:
            - ``services/core/video_frame.py:VideoFrameStore._bitstream_path``
            - ``services/core/video_frame.py:VideoFrameStore.save_bitstream``
        """
        return self._cache_root / cover_hash(video_rel)

    def _bitstream_path(self, video_rel: str) -> Path:
        """位流缓存文件路径.

        Args:
            video_rel: 相对数据集根的视频路径.

        Returns:
            Path: 位流缓存文件路径.
        Callers:
            - ``services/core/video_frame.py:VideoFrameStore.load_bitstream``
            - ``services/core/video_frame.py:VideoFrameStore.save_bitstream``
        """
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
            - ``services/core/video_frame.py:VideoFrameStore.analyze_bitstream``
            - ``routes/video.py:video_detail``
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
            - ``services/core/video_frame.py:VideoFrameStore.load_bitstream``
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

        Returns:
            None

        Callers:
            - ``services/core/video_frame.py:VideoFrameStore.load_bitstream``
            - ``services/core/video_frame.py:VideoFrameStore.analyze_bitstream``
        """
        d = self._cache_dir(video_rel)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._bitstream_path(video_rel), json.dumps(bits.model_dump()))

    def _stream_layout(self, path: Path, container, stream) -> _StreamLayout:
        """读取(带缓存)视频流的帧号几何.

        帧率取自容器 ``average_rate``; 首帧 pts 经首视频包扫描确定。
        缓存键计入文件 mtime 与 size, 视频被替换后自动失效。

        Args:
            path: 视频绝对路径.
            container: 已打开的 ``av.container.Container``(用于扫描首包).
            stream: ``av.video.stream.VideoStream`` 视频流.

        Returns:
            _StreamLayout: 帧号几何.

        Raises:
            VideoFrameError: 视频缺少有效帧率或无视频帧数据.

        Callers:
            - ``services/core/video_frame.py:VideoFrameStore.extract_frame``
            - ``services/core/video_frame.py:VideoFrameStore.segment_bytes``
        """
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        layout = self._layouts.get(key)
        if layout is not None:
            return layout
        rate = stream.average_rate
        if not rate or rate <= 0:
            raise VideoFrameError("视频缺少有效帧率")
        min_pts = None
        for pkt in container.demux(stream):
            if pkt.dts is None or pkt.pts is None:
                continue
            min_pts = pkt.pts
            break
        if min_pts is None:
            raise VideoFrameError("视频无视频帧数据")
        # 帧间隔以流 time_base 计: 1 帧(秒) / time_base(秒/tick) = 1/(rate·time_base) tick
        layout = _StreamLayout(min_pts, Fraction(1) / (rate * stream.time_base))
        self._layouts[key] = layout
        return layout

    def _configure_decode(self, stream) -> None:
        """配置视频流的解码线程模式与 AVOption.

        Args:
            stream: ``av.video.stream.VideoStream`` 视频流.

        Returns:
            None

        Callers:
            - ``services/core/video_frame.py:VideoFrameStore.extract_frame``
            - ``services/core/video_frame.py:VideoFrameStore.analyze_bitstream``
        """
        stream.thread_type = _THREAD_TYPE
        stream.thread_count = 0  # 按核数自动
        stream.codec_context.options = _DECODE_OPTIONS

    def segment_bytes(
        self,
        video_rel: str,
        start: int,
        end: int,
    ) -> tuple[bytes, int, int]:
        """把 ``[start, end]`` 帧窗口流复制为 H.264 Annex-B 字节流(不重编码).

        从 ``start`` 前最近的关键帧起按解码顺序复制压缩包(GOP 对齐), 前端解码
        时跳过未请求的前导帧; 段尾额外复制 ``_B_REORDER_MARGIN`` 帧, 保证窗口
        末帧(B 帧)的后向参考包完整。包经 ``h264_mp4toannexb`` 位流滤波器转为
        Annex-B 格式(WebCodecs 解码所需的 NAL 起始码形式), 段自包含可从头解码。
        实际复制帧数可能少于请求数(视频末尾), 通过返回值告知调用方。

        Args:
            video_rel: 相对数据集根的视频路径.
            start: 窗口起始帧索引.
            end: 窗口结束帧索引.

        Returns:
            ``(data, seg_start, count)``: Annex-B 裸流、段首帧的全局帧号与实际
            复制帧数(含段尾参考帧)。

        Raises:
            VideoFrameError: 视频无法打开或窗口内无关键帧.

        Callers:
            - ``routes/video.py:video_segment``
        """
        path = self._video_path(video_rel)
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            layout = self._stream_layout(path, container, stream)
            container.seek(
                int(layout.min_pts + start * layout.frame_dur),
                stream=stream, backward=True,
            )
            bsf = BitStreamFilterContext("h264_mp4toannexb", stream)
            out = bytearray()
            seg_start: int | None = None
            copied = 0
            margin_fno = end + _B_REORDER_MARGIN
            for pkt in container.demux(stream):
                if pkt.dts is None:
                    continue
                if seg_start is None and not pkt.is_keyframe:
                    # seek 定位点之后、首个关键帧之前的残余包不可解码, 跳过
                    continue
                if pkt.pts is None:
                    raise VideoFrameError("视频包缺失 pts, 无法换算帧号")
                # BSF 接管并清空原包(pts 归 None), 帧号须在 filter 前换算
                fno = _frame_no(pkt.pts, layout)
                if seg_start is None:
                    seg_start = fno
                for annex in bsf.filter(pkt):
                    out += bytes(annex)
                copied += 1
                if fno >= margin_fno:
                    break
            for annex in bsf.filter(None):
                out += bytes(annex)
        if seg_start is None or copied == 0:
            raise VideoFrameError(f"帧窗口 {start}..{end} 内无关键帧, 无法流复制")
        return bytes(out), seg_start, copied

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
            - ``services/core/video_frame.py:VideoFrameStore.extract_frame``
            - ``services/core/video_frame.py:VideoFrameStore.segment_bytes``
            - ``services/core/video_frame.py:VideoFrameStore.analyze_bitstream``
        """
        try:
            return resolve_under(self.data_dir, video_rel)
        except ValueError as e:
            raise VideoFrameError(str(e)) from e

    def extract_frame(self, video_rel: str, index: int) -> np.ndarray:
        """按帧索引读取一帧 BGR 图像.

        先 seek 到目标帧时刻之前最近的关键帧, 再顺序解码至帧号匹配的帧;
        解码输出按显示顺序, 与帧号语义一致。

        Args:
            video_rel: 相对数据集根的视频路径.
            index: 帧索引.

        Returns:
            np.ndarray: 单帧 BGR 图像.

        Raises:
            VideoFrameError: 视频无法打开或该帧不存在.

        Callers:
            - ``services/core/video_frame.py:VideoFrameStore.frame_bytes``
            - ``services/core/video_frame.py:VideoFrameStore.save_frame``
        """
        path = self._video_path(video_rel)
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            layout = self._stream_layout(path, container, stream)
            self._configure_decode(stream)
            container.seek(
                int(layout.min_pts + index * layout.frame_dur),
                stream=stream, backward=True,
            )
            for frame in container.decode(stream):
                if frame.pts is None:
                    raise VideoFrameError("解码输出帧缺失 pts, 无法换算帧号")
                fno = _frame_no(frame.pts, layout)
                if fno == index:
                    return frame.to_ndarray(format="bgr24")
                if fno > index:
                    break
        raise VideoFrameError(f"帧 {index} 不存在")

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
            - ``routes/video.py:video_frame``
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
        """拼接保存帧文件名.

        Args:
            stem: 视频名 stem.
            index: 帧索引.

        Returns:
            str: 形如 ``{stem}_frame_{index:06d}.png`` 的文件名.

        Callers:
            - ``services/core/video_frame.py:VideoFrameStore.save_frame``
            - ``services/core/video_frame.py:VideoFrameStore.remove_frame``
            - ``services/core/video_frame.py:VideoFrameStore.saved_indices``
        """
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
            - ``routes/video.py:save_frame``
        """
        frame = self.extract_frame(video_rel, index)
        stem = Path(video_rel).stem
        dest = self.out_dir / self.frame_name(stem, index)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            raise VideoFrameError("帧编码失败")
        atomic_write_bytes(dest, buf.tobytes())
        return dest

    def remove_frame(self, video_rel: str, index: int) -> bool:
        """删除已保存的指定帧 PNG.

        Args:
            video_rel: 相对数据集根的视频路径.
            index: 帧索引.

        Returns:
            bool: 是否删除了文件.

        Callers:
            - ``routes/video.py:delete_frame``
        """
        stem = Path(video_rel).stem
        dest = self.out_dir / self.frame_name(stem, index)
        if dest.is_file():
            dest.unlink()
            return True
        return False

    def saved_indices(self, video_rel: str) -> list[int]:
        """扫描输出目录, 返回该视频已保存的帧索引(升序).

        Args:
            video_rel: 相对数据集根的视频路径.

        Returns:
            list[int]: 已保存的帧索引升序列表; 输出目录不存在时为空.

        Callers:
            - ``routes/video.py:video_detail``
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
        """逐包解码视频, 提取每帧的编码类型(I/P/B)与包大小; 已分析的帧跳过.

        帧级 ``pict_type`` 仅存在于解码输出帧, 包级 flags 只有关键帧 K 标记,
        故须手动 demux 收集 ``{pts: 包大小}`` 后逐包解码, 经 pts 把解码帧与
        包大小配对(两者一一对应)。若缓存已存在则直接复用, 不重复扫描。包
        大小在持久化前量化到 KB, 与紧凑存储格式一致。

        Args:
            video_rel: 相对数据集根的视频路径.
            total: 视频总帧数(仅用于进度与校验).
            cancel_fn: 可选取消回调, 返回真值即停止.
            emit: 可选进度回调 ``(done, total)``.

        Returns:
            BitstreamTypes: 紧凑位流(``s`` 为量化到 KB 的大小).

        Raises:
            VideoFrameError: 视频无法打开、包大小配对失败或分析被取消.

        Callers:
            - ``routes/video.py:start_analysis``
        """
        path = self._video_path(video_rel)
        cached = self.load_bitstream(video_rel)
        if cached.t:
            if emit and total:
                emit(total, total)
            return cached

        t_chars: list[str] = []
        sizes: list[int] = []
        pkt_sizes: dict[int, int] = {}  # pts -> 包大小(字节); 经 pts 与解码帧配对
        cancelled = False

        def handle(frame) -> None:
            """收集解码帧的帧类型与配对的包大小, 并按需回报进度.

            Args:
                frame: ``av.video.frame.VideoFrame`` 解码输出帧(显示顺序).

            Returns:
                None

            Callers:
                - ``services/core/video_frame.py:VideoFrameStore.analyze_bitstream``
            """
            try:
                size = pkt_sizes.pop(frame.pts)
            except KeyError as e:
                raise VideoFrameError(f"包大小与解码帧配对失败(pts={frame.pts})") from e
            t_chars.append(_PICT_TYPE_CHARS.get(frame.pict_type, "?"))
            sizes.append(size // _SIZE_KB)
            if emit and total and len(sizes) % 50 == 0:
                emit(len(sizes), max(total, len(sizes)))

        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            self._configure_decode(stream)
            codec = stream.codec_context
            for pkt in container.demux(stream):
                if pkt.dts is None:
                    continue
                if pkt.pts is None:
                    raise VideoFrameError("视频包含缺失 pts 的包, 无法配对帧大小")
                pkt_sizes[pkt.pts] = pkt.size
                for frame in codec.decode(pkt):
                    if cancel_fn and cancel_fn():
                        cancelled = True
                        break
                    handle(frame)
                if cancelled:
                    break
            if not cancelled:
                # flush 解码器缓冲的末尾重排帧
                for frame in codec.decode(None):
                    if cancel_fn and cancel_fn():
                        cancelled = True
                        break
                    handle(frame)

        if cancelled:
            # 取消不持久化部分缓存, 以免下次复用残缺位流
            raise VideoFrameError("位流分析已取消")
        if emit and total:
            emit(max(total, len(sizes)), total)
        bits = BitstreamTypes(t="".join(t_chars), s=sizes)
        self.save_bitstream(video_rel, bits)
        return bits
