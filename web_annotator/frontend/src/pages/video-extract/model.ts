import { apiPath } from '@/lib/api';

/** 筛选页视频列表项(与后端 ``VideoInfo`` 对应)。 */
export interface VideoInfo {
  path: string;
  name: string;
  stem: string;
  width: number;
  height: number;
  duration: number;
  size: number;
  cover_hash: string;
}

/** 紧凑帧位流: ``chars`` 逐帧记录类型字符, ``sizes`` 为平行的包大小(KB)。 */
export interface FrameBitstream {
  chars: string;
  sizes: number[];
}

/** 视频详情(与后端 ``VideoDetail`` 对应)。 */
export interface VideoDetail {
  path: string;
  name: string;
  width: number;
  height: number;
  duration: number;
  total: number;
  types: { t: string; s: number[] };
  saved: number[];
}

/** 保存 / 删除帧响应(与后端 ``FrameSaveResponse`` 对应)。 */
export interface FrameSaveResponse {
  path: string;
  index: number;
  saved: number[];
  count: number;
}

/** 帧编码类型对应的柱状图颜色: 红 I / 黄 P / 蓝 B。 */
export const FRAME_COLORS: Record<string, string> = {
  I: '#ff6b6b',
  P: '#f5c542',
  B: '#4a9eff',
};

/** 已保存帧标记色。 */
export const SAVED_MARKER_COLOR = '#3ecf8e';

/** 缩略图条采样目标数量。 */
export const THUMB_COUNT = 60;

/** 缩略图条帧最长边像素。 */
export const THUMB_SIZE = 256;

/**
 * 构造抽帧缩略图地址。
 *
 * Args:
 *   path (string): 视频相对路径。
 *   index (number): 帧索引。
 *   size (number | undefined): 等比缩略最长边; 省略时返回原始尺寸。
 *
 * Returns:
 *   string: 抽帧接口地址。
 *
 * Callers:
 *   - `ThumbnailStrip`：缩略图与大图预览。
 */
export function frameUrl(path: string, index: number, size?: number): string {
  const base = `${apiPath('/video/frame')}?path=${encodeURIComponent(path)}&index=${index}`;
  return size ? `${base}&size=${size}` : base;
}

/**
 * 取指定帧的编码类型与包大小。
 *
 * Args:
 *   bits (FrameBitstream): 紧凑帧位流。
 *   index (number): 帧索引。
 *
 * Returns:
 *   { type: string; size: number } | null: 未分析或类型未知时为 null。
 *
 * Callers:
 *   - `VideoExtractPage`：状态栏与切帧过滤。
 *   - `VideoExtractPage`：隐藏 B 帧开关切换。
 */
export function frameTypeAt(
  bits: FrameBitstream,
  index: number,
): { type: string; size: number } | null {
  const type = bits.chars[index];
  if (!type || type === '?') return null;
  return { type, size: bits.sizes[index] ?? 0 };
}

/**
 * 判断位流中是否存在已分析的帧类型。
 *
 * Args:
 *   bits (FrameBitstream): 紧凑帧位流。
 *
 * Returns:
 *   boolean: 至少一帧含有效类型时为 true。
 *
 * Callers:
 *   - `VideoExtractPage`：详情加载后决定是否自动分析。
 */
export function hasFrameTypes(bits: FrameBitstream): boolean {
  for (let index = 0; index < bits.chars.length; index += 1) {
    const type = bits.chars[index];
    if (type && type !== '?') return true;
  }
  return false;
}

/**
 * 把秒数格式化为 ``m:ss``。
 *
 * Args:
 *   seconds (number): 秒数。
 *
 * Returns:
 *   string: 格式化文本; 非正数或非有限值返回空串。
 *
 * Callers:
 *   - `VideoExtractPage`：状态栏时间显示。
 */
export function formatTime(seconds: number): string {
  if (!(seconds > 0) || !Number.isFinite(seconds)) return '';
  const sec = Math.floor(seconds % 60);
  const min = Math.floor(seconds / 60);
  return `${min}:${sec < 10 ? '0' : ''}${sec}`;
}

/**
 * 读取地址栏中的 ``path`` 查询参数。
 *
 * Args:
 *   无。
 *
 * Returns:
 *   string: 解码后的路径; 缺省为空串。
 *
 * Callers:
 *   - `VideoExtractPage`：初始化时确定首个视频。
 */
export function readQueryPath(): string {
  const value = new URLSearchParams(window.location.search).get('path');
  return value ? decodeURIComponent(value) : '';
}
