import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { FRAME_COLORS, SAVED_MARKER_COLOR, type FrameBitstream } from './model';

/** 图表区域高度(像素)。 */
const CHART_HEIGHT = 100;

/** 单张帧类型柱状图的属性。 */
interface FrameChartProps {
  /** 全局图为粗尺度(视口约 10 分钟), 局部图为细尺度(视口约 200 帧)。 */
  mode: 'global' | 'local';
  /** 总帧数。 */
  total: number;
  /** 视频时长(秒)。 */
  duration: number;
  /** 紧凑帧位流。 */
  bits: FrameBitstream;
  /** 已保存帧索引。 */
  saved: number[];
  /** 当前帧索引。 */
  index: number;
  /** 是否隐藏 B 帧。 */
  hideB: boolean;
  /** 点击柱状图跳帧回调。 */
  onSeek: (index: number) => void;
}

/** 柱状图柱组属性。 */
interface BarsProps {
  slots: number[];
  bits: FrameBitstream;
  ppf: number;
  maxSize: number;
}

/** 已保存帧标记组属性。 */
interface SavedMarksProps {
  marks: Array<{ key: string; x: number }>;
  ppf: number;
}

/**
 * 渲染单张 SVG 柱状图的全部帧柱。
 *
 * 仅依赖位流、每帧像素宽与最大包大小, 换帧时不重新渲染, 避免逐帧遍历大量节点。
 *
 * Args:
 *   props (BarsProps): 可见帧索引、位流、每帧像素宽与最大包大小。
 *
 * Returns:
 *   JSX.Element: 柱组。
 *
 * Callers:
 *   - `FrameChart`：图表主体。
 */
const Bars = memo(function Bars({ slots, bits, ppf, maxSize }: BarsProps) {
  const plotHeight = CHART_HEIGHT - 2;
  const bottom = CHART_HEIGHT - 1;
  return (
    <g>
      {slots.map((frame, position) => {
        const height = ((bits.sizes[frame] ?? 0) / maxSize) * plotHeight;
        return (
          <rect
            key={frame}
            x={position * ppf}
            y={bottom - height}
            width={Math.max(1, ppf)}
            height={height}
            fill={FRAME_COLORS[bits.chars[frame]] ?? '#888'}
          />
        );
      })}
    </g>
  );
});

/**
 * 渲染已保存帧的顶部绿色刻度。
 *
 * Args:
 *   props (SavedMarksProps): 刻度横坐标与每帧像素宽。
 *
 * Returns:
 *   JSX.Element: 刻度组。
 *
 * Callers:
 *   - `FrameChart`：图表主体。
 */
const SavedMarks = memo(function SavedMarks({ marks, ppf }: SavedMarksProps) {
  return (
    <g>
      {marks.map((mark) => (
        <rect
          key={mark.key}
          x={mark.x}
          y={0}
          width={Math.max(2, ppf)}
          height={8}
          fill={SAVED_MARKER_COLOR}
        />
      ))}
    </g>
  );
});

/**
 * 计算已保存帧在显示域中的落点: 隐藏 B 帧时若该帧为 B 则向外搜索最近可见帧。
 *
 * Args:
 *   index (number): 已保存帧索引。
 *   bits (FrameBitstream): 紧凑帧位流。
 *   total (number): 总帧数。
 *   hideB (boolean): 是否隐藏 B 帧。
 *
 * Returns:
 *   number: 可见帧的全局索引。
 *
 * Callers:
 *   - `FrameChart`：已保存帧刻度定位。
 */
function nearestVisibleIndex(
  index: number,
  bits: FrameBitstream,
  total: number,
  hideB: boolean,
): number {
  if (!hideB) return index;
  const type = bits.chars[index];
  if (type && type !== 'B') return index;
  for (let distance = 1; distance < total; distance += 1) {
    const before = index - distance;
    const after = index + distance;
    const beforeType = before >= 0 ? bits.chars[before] : '';
    if (beforeType && beforeType !== '?' && beforeType !== 'B') return before;
    const afterType = after < total ? bits.chars[after] : '';
    if (afterType && afterType !== '?' && afterType !== 'B') return after;
  }
  return index;
}

/**
 * 单张帧类型柱状图: 每帧一根柱, 叠加已保存刻度与当前帧游标。
 *
 * SVG 宽度覆盖整条时间轴, 超出视口部分由容器原生横向滚动; 滚动只浏览,
 * 不改变当前帧。点击柱状图按每帧像素宽反查帧索引并跳转。
 *
 * Args:
 *   props (FrameChartProps): 尺度模式、位流、已保存帧、当前帧与跳帧回调。
 *
 * Returns:
 *   JSX.Element: 可横向滚动的图表容器。
 *
 * Callers:
 *   - `VideoExtractPage`：全局图与局部图并排展示。
 */
export function FrameChart({
  mode,
  total,
  duration,
  bits,
  saved,
  index,
  hideB,
  onSeek,
}: FrameChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setWidth(element.clientWidth));
    observer.observe(element);
    setWidth(element.clientWidth);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.deltaY) return;
      event.preventDefault();
      element.scrollLeft += event.deltaY;
    };
    element.addEventListener('wheel', onWheel, { passive: false });
    return () => element.removeEventListener('wheel', onWheel);
  }, []);

  const { slots, prefix, visibleCount } = useMemo(() => {
    const nextSlots: number[] = [];
    const nextPrefix = new Int32Array(total + 1);
    for (let frame = 0; frame < total; frame += 1) {
      const type = bits.chars[frame];
      const analyzed = !!type && type !== '?';
      if (analyzed && (!hideB || type !== 'B')) nextSlots.push(frame);
      nextPrefix[frame + 1] = nextPrefix[frame] + (analyzed && type !== 'B' ? 1 : 0);
    }
    return { slots: nextSlots, prefix: nextPrefix, visibleCount: nextPrefix[total] };
  }, [bits, total, hideB]);

  const maxSize = useMemo(() => {
    let max = 1;
    for (let frame = 0; frame < total; frame += 1) {
      const type = bits.chars[frame];
      if (!type || type === '?') continue;
      const size = bits.sizes[frame] ?? 0;
      if (size > max) max = size;
    }
    return max;
  }, [bits, total]);

  const domain = hideB ? visibleCount : total;
  const ppf = useMemo(() => {
    const domainCount = domain || 1;
    if (mode === 'global') {
      const windowFrames = duration > 0
        ? Math.max(1, Math.round((600 * domainCount) / duration))
        : domainCount;
      return width / Math.min(windowFrames, domainCount);
    }
    return width / Math.min(200, domainCount);
  }, [mode, width, domain, duration]);

  const svgWidth = Math.max(width, Math.ceil(domain * ppf));
  const ordinal = hideB ? prefix[Math.min(Math.max(index, 0), total)] : index;
  const cursorX = ordinal * ppf + ppf / 2;

  const savedMarks = useMemo(
    () =>
      saved.map((frame) => {
        const visible = nearestVisibleIndex(frame, bits, total, hideB);
        const position = hideB ? prefix[Math.min(Math.max(visible, 0), total)] : visible;
        return { key: String(frame), x: position * ppf };
      }),
    [saved, bits, total, hideB, prefix, ppf],
  );

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element || !svgWidth) return;
    const target = cursorX - width / 2;
    element.scrollLeft = Math.max(0, Math.min(target, svgWidth - width));
  }, [cursorX, width, svgWidth]);

  return (
    <div
      ref={containerRef}
      className={cn(
        'h-24 min-w-0 flex-1 cursor-crosshair overflow-x-auto overflow-y-hidden border-t border-border bg-[#16213e]',
        mode === 'local' ? 'border-l' : '',
      )}
      style={{ scrollbarWidth: 'thin', scrollbarColor: '#2a2a4a transparent' }}
    >
      {width > 0 ? (
        <svg
          width={svgWidth}
          height={CHART_HEIGHT}
          style={{ display: 'block' }}
          onClick={(event) => {
            if (!total) return;
            const rect = event.currentTarget.getBoundingClientRect();
            const slot = Math.floor((event.clientX - rect.left) / ppf);
            onSeek(slots[slot] ?? total - 1);
          }}
        >
          <Bars slots={slots} bits={bits} ppf={ppf} maxSize={maxSize} />
          <SavedMarks marks={savedMarks} ppf={ppf} />
          <line
            x1={cursorX}
            x2={cursorX}
            y1={0}
            y2={CHART_HEIGHT}
            stroke="#ffffff"
            strokeWidth={1}
          />
        </svg>
      ) : null}
    </div>
  );
}
