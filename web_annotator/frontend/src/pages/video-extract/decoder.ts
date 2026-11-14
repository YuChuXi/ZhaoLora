import { apiPath } from '@/lib/api';

/** 帧窗口最大时长(秒), 目标帧前后各取一半。 */
const SEG_MAX_SEC = 10;
/** 已解码帧缓存上限(段内位置), 超出后按输出顺序淘汰。 */
const FRAME_CACHE = 60;
/** 回退重建解码器时, 目标帧之后追加喂入的包数, 覆盖 B 帧后向参考。 */
const FEED_MARGIN = 16;

/** 单个编码包: AVCC 长度前缀字节与是否关键帧。 */
interface EncodedChunk {
  bytes: Uint8Array;
  key: boolean;
}

/** 一个可独立解码的窗口段。 */
interface VideoSegment {
  /** 段首帧的全局帧号。 */
  start: number;
  /** 段尾帧的全局帧号。 */
  end: number;
  /** 按解码顺序排列的编码包。 */
  chunks: EncodedChunk[];
  /** 序列参数集。 */
  sps: Uint8Array;
  /** 图像参数集。 */
  pps: Uint8Array;
}

/** Annex-B 裸流解复用结果。 */
interface DemuxedStream {
  sps: Uint8Array | null;
  pps: Uint8Array | null;
  chunks: EncodedChunk[];
}

/** 解码控制器与页面交互所需的回调。 */
export interface VideoFrameDecoderOptions {
  /** 主预览画布。 */
  canvas: HTMLCanvasElement;
  /** 帧绘制完成回调, 参数为全局帧号与画面尺寸。 */
  onPaint: (index: number, width: number, height: number) => void;
  /** 窗口段加载状态变化回调。 */
  onLoadingChange: (loading: boolean) => void;
  /** 加载或解码失败回调。 */
  onError: (error: Error) => void;
}

/**
 * 视频窗口段的 WebCodecs 流式解码控制器。
 *
 * 后端按 ``[start, end]`` 帧窗口流复制源压缩包(不重编码), 段首为 start 前最近
 * 关键帧, 前导帧与段尾 B 帧参考帧由解码器自然消化。控制器按目标位置增量喂包
 * (喂到目标 + FEED_MARGIN, 不 flush), 输出帧按显示顺序回调并以段内位置作为
 * chunk timestamp; 已解码帧按 LRU 缓存, 段内向前切帧零重复解码, 回退超出缓存
 * 才从目标前最近关键帧重建解码器。VideoFrame 必须显式 close 归还解码器缓冲,
 * 否则累积会耗尽硬件解码 surface 池导致输出停摆。
 *
 * Callers:
 *   - `VideoExtractPage`：视频抽帧页主预览。
 */
export class VideoFrameDecoder {
  private readonly canvas: HTMLCanvasElement;

  private readonly context: CanvasRenderingContext2D;

  private readonly onPaint: VideoFrameDecoderOptions['onPaint'];

  private readonly onLoadingChange: VideoFrameDecoderOptions['onLoadingChange'];

  private readonly onError: VideoFrameDecoderOptions['onError'];

  /** 当前视频相对路径。 */
  private path = '';

  /** 当前视频总帧数。 */
  private total = 0;

  /** 当前视频时长(秒)。 */
  private duration = 0;

  private decoder: VideoDecoder | null = null;

  private segment: VideoSegment | null = null;

  /** 窗口段版本号, 换段递增以丢弃过期响应。 */
  private segmentSeq = 0;

  /** 段内位置到已解码帧的缓存。 */
  private frames = new Map<number, VideoFrame>();

  /** 缓存帧的输出顺序, 供 LRU 淘汰。 */
  private frameOrder: number[] = [];

  /** 已输出的最大段内位置(显示序单调)。 */
  private maxOut = -1;

  /** 已喂入解码器的最大段内位置(增量喂包游标)。 */
  private fedUntil = -1;

  /** 已执行过段尾 flush 的段内位置。 */
  private flushedEnd = -1;

  /** 请求中的全局帧索引, 过期目标丢弃。 */
  private pendingTarget = -1;

  /** 等待解码输出的段内位置。 */
  private pendingPos = -1;

  /** pendingPos 所属的段版本, 防止跨段回调误绘。 */
  private pendingSeq = -1;

  /** 在途请求覆盖的窗口及其完成 Promise。 */
  private inflight: { start: number; end: number; promise: Promise<void> } | null = null;

  /**
   * 绑定画布与回调。
   *
   * Args:
   *   options (VideoFrameDecoderOptions): 画布与生命周期回调。
   *
   * Returns:
   *   VideoFrameDecoder: 控制器实例。
   *
   * Callers:
   *   - `VideoExtractPage`：首次打开视频时创建。
   */
  constructor(options: VideoFrameDecoderOptions) {
    const context = options.canvas.getContext('2d');
    if (!context) throw new Error('无法获取画布 2D 上下文');
    this.canvas = options.canvas;
    this.context = context;
    this.onPaint = options.onPaint;
    this.onLoadingChange = options.onLoadingChange;
    this.onError = options.onError;
  }

  /**
   * 切换到新视频并清空全部解码状态。
   *
   * Args:
   *   path (string): 视频相对路径。
   *   total (number): 总帧数。
   *   duration (number): 时长(秒)。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：视频详情加载完成后调用。
   */
  setVideo(path: string, total: number, duration: number): void {
    this.reset();
    this.path = path;
    this.total = total;
    this.duration = duration;
  }

  /**
   * 请求显示指定全局帧号。
   *
   * Args:
   *   index (number): 目标帧索引。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：切帧、跳帧与首帧显示。
   */
  show(index: number): void {
    if (!this.total) return;
    this.pendingTarget = index;
    this.ensureSegment(index)
      .then(() => {
        if (this.pendingTarget !== index) return;
        this.displayAt(index - this.segment!.start, index);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        this.onError(new Error(`帧 ${index} 加载失败: ${message}`));
        this.onLoadingChange(false);
      });
  }

  /**
   * 关闭解码器并清空窗口段、缓存与挂起状态。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：切换视频前。
   *   - `VideoExtractPage`：组件卸载时。
   */
  reset(): void {
    this.closeDecoder();
    this.segment = null;
    this.inflight = null;
    this.segmentSeq += 1;
    this.clearFrames();
    this.fedUntil = -1;
    this.flushedEnd = -1;
    this.pendingTarget = -1;
    this.pendingPos = -1;
    this.pendingSeq = -1;
    this.path = '';
    this.total = 0;
    this.duration = 0;
  }

  /**
   * 释放解码器与全部缓存帧(组件卸载时调用)。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoExtractPage`：卸载清理。
   */
  destroy(): void {
    this.reset();
  }

  /**
   * 构造帧窗口段 URL。
   *
   * Args:
   *   start (number): 窗口起始帧。
   *   end (number): 窗口结束帧。
   *
   * Returns:
   *   string: 段请求地址。
   *
   * Callers:
   *   - `VideoFrameDecoder.ensureSegment`。
   */
  private segmentUrl(start: number, end: number): string {
    return (
      `${apiPath('/video/segment')}?path=${encodeURIComponent(this.path)}`
      + `&start=${start}&end=${end}`
    );
  }

  /**
   * 计算帧窗口半宽(帧数), 前后各最多 SEG_MAX_SEC/2 秒。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   number: 窗口半宽帧数。
   *
   * Callers:
   *   - `VideoFrameDecoder.ensureSegment`。
   */
  private segmentHalf(): number {
    if (this.duration > 0 && this.total > 0) {
      const fps = this.total / this.duration;
      return Math.max(30, Math.round((fps * SEG_MAX_SEC) / 2));
    }
    return 100;
  }

  /**
   * 拉取覆盖目标帧的窗口段并重建解码器。
   *
   * Args:
   *   index (number): 目标全局帧索引。
   *
   * Returns:
   *   Promise<void>: 段就绪后完成。
   *
   * Callers:
   *   - `VideoFrameDecoder.show`。
   */
  private ensureSegment(index: number): Promise<void> {
    if (this.segment && index >= this.segment.start && index <= this.segment.end) {
      return Promise.resolve();
    }
    if (this.inflight && index >= this.inflight.start && index <= this.inflight.end) {
      return this.inflight.promise;
    }
    const half = this.segmentHalf();
    const start = Math.max(0, index - half);
    const end = Math.min(this.total - 1, index + half);
    const seq = this.segmentSeq + 1;
    this.segmentSeq = seq;
    this.onLoadingChange(true);
    const promise = fetch(this.segmentUrl(start, end))
      .then((response) => {
        if (!response.ok) throw new Error(`段获取失败 ${response.status}`);
        const count = Number.parseInt(response.headers.get('X-Frame-Count') ?? '', 10) || 0;
        const segmentStart = Number.parseInt(response.headers.get('X-Segment-Start') ?? '', 10);
        return response.arrayBuffer().then((buffer) => ({ buffer, count, segmentStart }));
      })
      .then((result) => {
        if (seq !== this.segmentSeq) return;
        const parsed = demuxAnnexB(new Uint8Array(result.buffer));
        if (!parsed.sps || !parsed.pps || !parsed.chunks.length) {
          throw new Error('段内无有效编码帧');
        }
        this.closeDecoder();
        this.decoder = this.makeDecoder();
        this.decoder.configure({
          codec: buildCodecString(parsed.sps),
          description: buildAvcConfig(parsed.sps, parsed.pps),
        });
        this.segment = {
          start: result.segmentStart,
          end: result.segmentStart + result.count - 1,
          chunks: parsed.chunks,
          sps: parsed.sps,
          pps: parsed.pps,
        };
        this.inflight = null;
        this.clearFrames();
        this.pendingPos = -1;
        this.fedUntil = -1;
        this.flushedEnd = -1;
      })
      .catch((error: unknown) => {
        if (seq === this.segmentSeq) this.inflight = null;
        throw error instanceof Error ? error : new Error(String(error));
      });
    this.inflight = { start, end, promise };
    return promise;
  }

  /**
   * 向解码器喂入段内 [from, to] 位置的编码包(不 flush)。
   *
   * Args:
   *   from (number): 起始段内位置。
   *   to (number): 结束段内位置。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoFrameDecoder.displayAt`。
   *   - `VideoFrameDecoder.redecode`。
   */
  private feedChunks(from: number, to: number): void {
    const segment = this.segment;
    if (!segment || !this.decoder) return;
    for (let position = from; position <= to && position < segment.chunks.length; position += 1) {
      const chunk = segment.chunks[position];
      this.decoder.decode(
        new EncodedVideoChunk({
          type: chunk.key ? 'key' : 'delta',
          timestamp: position,
          data: chunk.bytes,
        }),
      );
    }
  }

  /**
   * 显示段内位置的帧: 缓存命中即绘, 未输出则增量喂包等待回调, 已被淘汰则重建解码器。
   *
   * Args:
   *   position (number): 窗口内目标位置(显示顺序)。
   *   index (number): 全局帧索引。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoFrameDecoder.show`。
   */
  private displayAt(position: number, index: number): void {
    const cached = this.frames.get(position);
    if (cached) {
      this.paintFrame(cached, index);
      return;
    }
    if (position > this.maxOut) {
      const segment = this.segment!;
      const last = segment.chunks.length - 1;
      const to = Math.min(position + FEED_MARGIN, last);
      if (to > this.fedUntil) {
        this.feedChunks(this.fedUntil + 1, to);
        this.fedUntil = to;
      }
      if (to === last && this.flushedEnd !== last) {
        this.flushedEnd = last;
        void this.decoder!.flush();
      }
      this.pendingPos = position;
      this.pendingSeq = this.segmentSeq;
      return;
    }
    this.redecode(position);
  }

  /**
   * 关闭并清空全部缓存帧。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoFrameDecoder.ensureSegment`。
   *   - `VideoFrameDecoder.redecode`。
   *   - `VideoFrameDecoder.reset`。
   */
  private clearFrames(): void {
    for (const frame of this.frames.values()) frame.close();
    this.frames.clear();
    this.frameOrder = [];
    this.maxOut = -1;
  }

  /**
   * 关闭当前解码器(已关闭时跳过)。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoFrameDecoder.ensureSegment`。
   *   - `VideoFrameDecoder.redecode`。
   *   - `VideoFrameDecoder.reset`。
   */
  private closeDecoder(): void {
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
    this.decoder = null;
  }

  /**
   * 查找窗口内最近一个关键帧位置。
   *
   * Args:
   *   position (number): 段内目标位置。
   *
   * Returns:
   *   number: 最近关键帧的段内位置; 无则 0。
   *
   * Callers:
   *   - `VideoFrameDecoder.redecode`。
   */
  private lastKeyAtOrBefore(position: number): number {
    const chunks = this.segment!.chunks;
    for (let index = position; index >= 0; index -= 1) {
      if (chunks[index]?.key) return index;
    }
    return 0;
  }

  /**
   * 构造解码器, 解码结果交由输出回调收集。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   VideoDecoder: 新解码器实例。
   *
   * Callers:
   *   - `VideoFrameDecoder.ensureSegment`。
   *   - `VideoFrameDecoder.redecode`。
   */
  private makeDecoder(): VideoDecoder {
    return new VideoDecoder({
      output: (frame) => this.onDecoded(frame),
      error: (error) => this.onError(new Error(`视频解码失败: ${error.message}`)),
    });
  }

  /**
   * 回退到已被淘汰的帧: 重建解码器并从目标前最近关键帧重新喂包。
   *
   * Args:
   *   position (number): 段内目标位置。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoFrameDecoder.displayAt`。
   */
  private redecode(position: number): void {
    const segment = this.segment!;
    this.closeDecoder();
    this.decoder = this.makeDecoder();
    this.decoder.configure({
      codec: buildCodecString(segment.sps),
      description: buildAvcConfig(segment.sps, segment.pps),
    });
    this.clearFrames();
    this.flushedEnd = -1;
    const last = segment.chunks.length - 1;
    const to = Math.min(position + FEED_MARGIN, last);
    this.feedChunks(this.lastKeyAtOrBefore(position), to);
    this.fedUntil = to;
    if (to === last) {
      this.flushedEnd = last;
      void this.decoder.flush();
    }
    this.pendingPos = position;
    this.pendingSeq = this.segmentSeq;
  }

  /**
   * 解码输出回调: 帧 timestamp 即段内位置, 帧进入 LRU 缓存并在目标到达时绘制。
   *
   * Args:
   *   frame (VideoFrame): 解码输出帧(显示顺序)。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoFrameDecoder.makeDecoder`。
   */
  private onDecoded(frame: VideoFrame): void {
    const position = frame.timestamp;
    const existing = this.frames.get(position);
    if (existing) existing.close();
    this.frames.set(position, frame);
    this.frameOrder.push(position);
    if (position > this.maxOut) this.maxOut = position;
    while (this.frameOrder.length > FRAME_CACHE) {
      const oldPosition = this.frameOrder.shift()!;
      const old = this.frames.get(oldPosition);
      this.frames.delete(oldPosition);
      if (old) old.close();
    }
    if (this.pendingPos === position && this.pendingSeq === this.segmentSeq) {
      this.pendingPos = -1;
      const index = this.segment ? this.segment.start + position : -1;
      if (index >= 0 && index === this.pendingTarget) this.paintFrame(frame, index);
    }
  }

  /**
   * 把解码帧绘制到主预览画布并上报尺寸。
   *
   * Args:
   *   frame (VideoFrame): 待绘制帧。
   *   index (number): 全局帧索引。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `VideoFrameDecoder.displayAt`。
   *   - `VideoFrameDecoder.onDecoded`。
   */
  private paintFrame(frame: VideoFrame, index: number): void {
    const width = frame.displayWidth;
    const height = frame.displayHeight;
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.context.drawImage(frame, 0, 0, width, height);
    this.pendingTarget = index;
    this.onPaint(index, width, height);
  }
}

/**
 * 解析 H.264 Annex-B 裸流: 提取 SPS/PPS 并按片 NAL 分组为帧。
 *
 * SPS/PPS 仅用于 configure 的 description, 不进入 chunk; 片 NAL 为 IDR(类型 5)
 * 即关键帧。
 *
 * Args:
 *   bin (Uint8Array): Annex-B 裸流字节。
 *
 * Returns:
 *   DemuxedStream: 参数集与按解码顺序排列的编码包。
 *
 * Callers:
 *   - `VideoFrameDecoder.ensureSegment`。
 */
function demuxAnnexB(bin: Uint8Array): DemuxedStream {
  const nals: Uint8Array[] = [];
  let index = 0;
  const length = bin.length;
  while (index < length) {
    let startCodeLength = 0;
    if (index + 3 <= length && bin[index] === 0 && bin[index + 1] === 0 && bin[index + 2] === 1) {
      startCodeLength = 3;
    } else if (
      index + 4 <= length
      && bin[index] === 0
      && bin[index + 1] === 0
      && bin[index + 2] === 0
      && bin[index + 3] === 1
    ) {
      startCodeLength = 4;
    }
    if (!startCodeLength) {
      index += 1;
      continue;
    }
    index += startCodeLength;
    const start = index;
    while (index < length) {
      if (index + 3 <= length && bin[index] === 0 && bin[index + 1] === 0 && bin[index + 2] === 1) break;
      if (
        index + 4 <= length
        && bin[index] === 0
        && bin[index + 1] === 0
        && bin[index + 2] === 0
        && bin[index + 3] === 1
      ) {
        break;
      }
      index += 1;
    }
    nals.push(bin.subarray(start, index));
  }

  let sps: Uint8Array | null = null;
  let pps: Uint8Array | null = null;
  for (const nal of nals) {
    const type = nal[0] & 0x1f;
    if (type === 7 && !sps) sps = nal;
    else if (type === 8 && !pps) pps = nal;
  }

  const chunks: EncodedChunk[] = [];
  for (const nal of nals) {
    const type = nal[0] & 0x1f;
    if (type === 1 || type === 5) {
      chunks.push({ bytes: toLengthPrefixed([nal]), key: type === 5 });
    }
  }
  return { sps, pps, chunks };
}

/**
 * 把一组 NAL 封装为 4 字节长度前缀的 AVCC 数据。
 *
 * Args:
 *   nals (Uint8Array[]): 待封装的 NAL 切片。
 *
 * Returns:
 *   Uint8Array: AVCC 字节。
 *
 * Callers:
 *   - `demuxAnnexB`。
 */
function toLengthPrefixed(nals: Uint8Array[]): Uint8Array {
  const total = nals.reduce((sum, nal) => sum + 4 + nal.length, 0);
  const out = new Uint8Array(total);
  const view = new DataView(out.buffer);
  let offset = 0;
  for (const nal of nals) {
    view.setUint32(offset, nal.length);
    out.set(nal, offset + 4);
    offset += 4 + nal.length;
  }
  return out;
}

/**
 * 由 SPS 构造 avc1 codec 字符串(profile.compat.level)。
 *
 * Args:
 *   sps (Uint8Array): 序列参数集。
 *
 * Returns:
 *   string: WebCodecs 编码器标识。
 *
 * Callers:
 *   - `VideoFrameDecoder.ensureSegment`。
 *   - `VideoFrameDecoder.redecode`。
 */
function buildCodecString(sps: Uint8Array): string {
  const hex = (value: number) => value.toString(16).padStart(2, '0');
  return `avc1.${hex(sps[1])}${hex(sps[2])}${hex(sps[3])}`;
}

/**
 * 由 SPS/PPS 构造 AVCDecoderConfigurationRecord 作为解码器 description。
 *
 * Args:
 *   sps (Uint8Array): 序列参数集。
 *   pps (Uint8Array): 图像参数集。
 *
 * Returns:
 *   Uint8Array: AVCDecoderConfigurationRecord 字节。
 *
 * Callers:
 *   - `VideoFrameDecoder.ensureSegment`。
 *   - `VideoFrameDecoder.redecode`。
 */
function buildAvcConfig(sps: Uint8Array, pps: Uint8Array): Uint8Array {
  const config = new Uint8Array(11 + sps.length + pps.length);
  const view = new DataView(config.buffer);
  view.setUint8(0, 1);
  view.setUint8(1, sps[1]);
  view.setUint8(2, sps[2]);
  view.setUint8(3, sps[3]);
  view.setUint8(4, 0xff);
  view.setUint8(5, 0xe1);
  view.setUint16(6, sps.length);
  config.set(sps, 8);
  const offset = 8 + sps.length;
  view.setUint8(offset, 1);
  view.setUint16(offset + 1, pps.length);
  config.set(pps, offset + 3);
  return config;
}
