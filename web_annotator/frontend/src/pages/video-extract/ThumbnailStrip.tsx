import { useEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { THUMB_COUNT, THUMB_SIZE, frameUrl } from './model';

/** 缩略图条属性。 */
interface ThumbnailStripProps {
  /** 当前视频相对路径。 */
  path: string;
  /** 总帧数。 */
  total: number;
  /** 视频宽度。 */
  width: number;
  /** 视频高度。 */
  height: number;
  /** 当前帧索引。 */
  index: number;
  /** 已保存帧集合。 */
  saved: Set<number>;
  /** 已勾选帧集合。 */
  checked: Set<number>;
  /** 点击缩略图跳帧回调。 */
  onSeek: (index: number) => void;
  /** 切换帧勾选状态回调。 */
  onToggleCheck: (index: number) => void;
}

/**
 * 按采样间隔构建缩略图帧号列表(目标 THUMB_COUNT 个, 末尾补最后一帧)。
 *
 * Args:
 *   total (number): 总帧数。
 *
 * Returns:
 *   number[]: 升序采样帧号。
 *
 * Callers:
 *   - `ThumbnailStrip`：缩略图构建。
 */
function sampleIndices(total: number): number[] {
  if (!total) return [];
  const interval = Math.max(1, Math.round(total / THUMB_COUNT));
  const indices: number[] = [];
  for (let frame = 0; frame < total; frame += interval) indices.push(frame);
  if (indices[indices.length - 1] !== total - 1) indices.push(total - 1);
  return indices;
}

/**
 * 缩略图条: 采样帧快速跳转、帧勾选与悬停大图预览。
 *
 * Args:
 *   props (ThumbnailStripProps): 视频尺寸、当前帧、已保存/已勾选集合与交互回调。
 *
 * Returns:
 *   JSX.Element: 可横向滚动的缩略图条与固定定位预览层。
 *
 * Callers:
 *   - `VideoExtractPage`：主预览下方缩略图条。
 */
export function ThumbnailStrip({
  path,
  total,
  width,
  height,
  index,
  saved,
  checked,
  onSeek,
  onToggleCheck,
}: ThumbnailStripProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const layerRef = useRef<HTMLDivElement>(null);
  const timer = useRef<number>();
  const [preview, setPreview] = useState<{ index: number; src: string } | null>(null);

  const indices = useMemo(() => sampleIndices(total), [total]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.deltaY) return;
      event.preventDefault();
      container.scrollLeft += event.deltaY;
    };
    container.addEventListener('wheel', onWheel, { passive: false });
    return () => container.removeEventListener('wheel', onWheel);
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    container
      .querySelector<HTMLElement>(`[data-frame-index="${index}"]`)
      ?.scrollIntoView({ block: 'nearest', inline: 'center' });
  }, [index, total]);

  useEffect(() => {
    window.clearTimeout(timer.current);
    setPreview(null);
  }, [path]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  /**
   * 把预览层定位到鼠标上方(空间不足时移到下方), 并限制在视口内。
   *
   * Args:
   *   clientX (number): 鼠标横坐标。
   *   clientY (number): 鼠标纵坐标。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ThumbnailStrip`：悬停鼠标移动。
   */
  const positionPreview = (clientX: number, clientY: number) => {
    const layer = layerRef.current;
    if (!layer) return;
    const layerWidth = layer.offsetWidth;
    const layerHeight = layer.offsetHeight;
    const x = Math.min(Math.max(clientX - layerWidth / 2, 8), window.innerWidth - layerWidth - 8);
    let y = clientY - layerHeight - 14;
    if (y < 8) y = clientY + 18;
    layer.style.left = `${x}px`;
    layer.style.top = `${y}px`;
  };

  return (
    <div className="relative flex-none">
      <div
        ref={containerRef}
        className="flex items-center gap-1.5 overflow-x-auto border-t border-border bg-[#16213e] px-3 py-2"
        style={{ scrollbarWidth: 'thin', scrollbarColor: '#2a2a4a transparent' }}
        onMouseOver={(event) => {
          const box = (event.target as HTMLElement).closest<HTMLElement>('[data-frame-index]');
          if (!box) return;
          const frame = Number(box.dataset.frameIndex);
          if (!Number.isFinite(frame)) return;
          setPreview((current) => ({ index: frame, src: current?.src ?? '' }));
          window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => {
            setPreview((current) => (current ? { ...current, src: frameUrl(path, frame, 480) } : current));
          }, 100);
        }}
        onMouseMove={(event) => positionPreview(event.clientX, event.clientY)}
        onMouseOut={(event) => {
          if ((event.target as HTMLElement).closest('[data-frame-index]')) {
            window.clearTimeout(timer.current);
            setPreview(null);
          }
        }}
      >
        {indices.map((frame) => {
          const active = frame === index;
          const isSaved = saved.has(frame);
          const isChecked = checked.has(frame);
          return (
            <div
              key={frame}
              data-frame-index={frame}
              className={cn(
                'relative h-14 flex-none cursor-pointer overflow-hidden rounded-sm border-2 bg-[#0a1a30] hover:border-[#8a8a9a]',
                active || isSaved ? '' : 'border-transparent',
              )}
              style={{
                borderColor: active ? '#4ade80' : isSaved ? '#e94560' : undefined,
                boxShadow: active && isSaved ? '0 0 0 1px #e94560' : undefined,
              }}
              onClick={() => onSeek(frame)}
            >
              <img
                src={frameUrl(path, frame, THUMB_SIZE)}
                alt=""
                loading="lazy"
                className="block h-[52px] w-auto rounded-sm bg-[#0a1a30]"
                style={{ aspectRatio: `${width || 16} / ${height || 9}` }}
              />
              <button
                type="button"
                aria-label="勾选该帧"
                aria-pressed={isChecked}
                title="勾选该帧"
                className={cn(
                  'absolute left-0.5 top-0.5 flex h-4 w-4 items-center justify-center rounded-sm border text-[10px] leading-none',
                  isChecked
                    ? 'border-primary bg-primary text-white'
                    : 'border-white/50 bg-black/60 text-transparent',
                )}
                onMouseDown={(event) => event.stopPropagation()}
                onClick={(event) => {
                  event.stopPropagation();
                  onToggleCheck(frame);
                }}
              >
                ✓
              </button>
              <span className="absolute bottom-0.5 right-0.5 rounded bg-black/70 px-1 text-[10px] text-white">
                {frame}
              </span>
            </div>
          );
        })}
      </div>
      <div
        ref={layerRef}
        className="pointer-events-none fixed z-50 rounded border border-border bg-[#16213e] p-1 shadow-xl"
        style={{ display: preview ? 'block' : 'none' }}
      >
        {preview?.src ? (
          <img
            src={preview.src}
            alt=""
            className="block max-h-[320px] max-w-[480px] rounded-sm bg-[#0a1a30]"
          />
        ) : null}
        <span className="absolute bottom-1.5 right-1.5 rounded bg-black/70 px-1 text-[11px] text-white">
          {preview?.index ?? ''}
        </span>
      </div>
    </div>
  );
}
