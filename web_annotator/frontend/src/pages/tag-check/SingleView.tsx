import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { Filmstrip } from './Filmstrip';
import type { TagCheckItem } from './model';

interface SingleViewProps {
  items: TagCheckItem[];
  index: number;
  fit: 'contain' | 'cover';
  stripOpen: boolean;
  onSelectIndex: (index: number) => void;
  onToggleStrip: () => void;
  onAdd: () => void;
  onDelete: () => void;
}

interface LensState {
  left: number;
  top: number;
  backgroundSize: string;
  backgroundPosition: string;
}

const LENS_SIZE = 180;

/**
 * 计算图片在视口中的实际显示矩形；contain 模式下扣除 object-fit 产生的留白。
 *
 * Args:
 *   image (HTMLImageElement): 单图元素。
 *   fit ('contain' | 'cover'): 当前适配模式。
 *
 * Returns:
 *   { left: number; top: number; width: number; height: number } | null: 显示矩形；图片尚未加载时为 null。
 *
 * Callers:
 *   - `pages/tag-check/SingleView.tsx:SingleView`
 */
function displayedRect(
  image: HTMLImageElement,
  fit: 'contain' | 'cover',
): { left: number; top: number; width: number; height: number } | null {
  const bounds = image.getBoundingClientRect();
  if (fit === 'cover') {
    return { left: bounds.left, top: bounds.top, width: bounds.width, height: bounds.height };
  }
  const naturalWidth = image.naturalWidth;
  const naturalHeight = image.naturalHeight;
  if (!naturalWidth || !naturalHeight) return null;
  const scale = Math.min(bounds.width / naturalWidth, bounds.height / naturalHeight);
  const width = naturalWidth * scale;
  const height = naturalHeight * scale;
  return {
    left: bounds.left + (bounds.width - width) / 2,
    top: bounds.top + (bounds.height - height) / 2,
    width,
    height,
  };
}

/**
 * 单图视图：主图区、跟随鼠标的放大镜、横排预览条与底部状态栏。
 *
 * 左键点击主图添加标签，右键删除标签；上一张/下一张、fit 切换与返回由工具栏按钮
 * 和全局快捷键驱动。
 *
 * Args:
 *   items (TagCheckItem[]): 当前结果列表。
 *   index (number): 当前图片索引。
 *   fit ('contain' | 'cover'): 图片适配模式。
 *   stripOpen (boolean): 预览条是否展开。
 *   onSelectIndex (function): 点击预览条缩略图的跳转回调。
 *   onToggleStrip (function): 点击位置分段切换预览条的回调。
 *   onAdd (function): 左键添加标签回调。
 *   onDelete (function): 右键删除标签回调。
 *
 * Returns:
 *   JSX.Element | null: 单图视图节点；索引非法时为 null。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 */
export function SingleView({
  items,
  index,
  fit,
  stripOpen,
  onSelectIndex,
  onToggleStrip,
  onAdd,
  onDelete,
}: SingleViewProps) {
  const stageRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const zoomRef = useRef(4);
  const fitRef = useRef(fit);
  const lensRef = useRef<LensState | null>(null);
  const computeRef = useRef<(clientX: number, clientY: number) => void>(() => {});
  const centerOnLoad = useRef(true);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const [lens, setLens] = useState<LensState | null>(null);

  fitRef.current = fit;
  lensRef.current = lens;

  useEffect(() => {
    centerOnLoad.current = true;
  }, [fit]);

  useEffect(() => {
    setLens(null);
  }, [index]);

  /**
   * 依据鼠标位置计算放大镜的位置、缩放与背景偏移。
   *
   * Args:
   *   clientX (number): 鼠标视口横坐标。
   *   clientY (number): 鼠标视口纵坐标。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/SingleView.tsx:SingleView` 主图区 mousemove
   *   - `pages/tag-check/SingleView.tsx:SingleView` 滚轮缩放
   *   - `pages/tag-check/SingleView.tsx:SingleView` 主图区滚动重定位
   */
  const updateLens = (clientX: number, clientY: number) => {
    const image = imageRef.current;
    if (!image) return;
    const rect = displayedRect(image, fitRef.current);
    if (!rect) {
      setLens(null);
      return;
    }
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) {
      setLens(null);
      return;
    }
    const naturalWidth = image.naturalWidth;
    const naturalHeight = image.naturalHeight;
    if (!naturalWidth || !naturalHeight) {
      setLens(null);
      return;
    }
    const zoom = zoomRef.current;
    let left = clientX - LENS_SIZE / 2;
    let top = clientY - LENS_SIZE - 16;
    if (top < 0) top = clientY + 16;
    left = Math.max(0, Math.min(left, window.innerWidth - LENS_SIZE));
    top = Math.max(0, Math.min(top, window.innerHeight - LENS_SIZE));
    const originX = (x / rect.width) * naturalWidth;
    const originY = (y / rect.height) * naturalHeight;
    setLens({
      left,
      top,
      backgroundSize: `${naturalWidth * zoom}px ${naturalHeight * zoom}px`,
      backgroundPosition: `${-(originX * zoom - LENS_SIZE / 2)}px ${-(originY * zoom - LENS_SIZE / 2)}px`,
    });
  };
  computeRef.current = updateLens;

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (event: WheelEvent) => {
      if (!lensRef.current) return;
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.25 : 0.8;
      zoomRef.current = Math.max(1, Math.min(32, zoomRef.current * factor));
      computeRef.current(event.clientX, event.clientY);
    };
    stage.addEventListener('wheel', onWheel, { passive: false });
    return () => stage.removeEventListener('wheel', onWheel);
  }, []);

  const item = items[index];
  if (!item) return null;

  const imageSrc = api.localImageUrl(item.path);
  const ok = item.exists && item.position_ok;
  const bad = item.exists && !item.position_ok;
  const frame = ok ? '#4ade80' : bad ? '#e94560' : undefined;
  const stageShadow = ok
    ? 'inset 0 0 0 3px #4ade80, inset 0 0 40px rgba(74,222,128,0.15)'
    : bad
      ? 'inset 0 0 0 3px #e94560, inset 0 0 40px rgba(233,69,96,0.15)'
      : undefined;
  const okCount = items.reduce((total, entry) => total + (entry.position_ok ? 1 : 0), 0);
  const statusText = !item.exists ? '无此标签' : item.position_ok ? '位置符合' : '位置不符';

  return (
    <div className="absolute inset-0 z-20 flex flex-col bg-background">
      <div
        ref={stageRef}
        className={
          fit === 'contain'
            ? 'relative min-h-0 flex-1 cursor-crosshair overflow-hidden bg-[#05070d]'
            : 'relative min-h-0 flex-1 cursor-crosshair overflow-auto bg-[#05070d]'
        }
        style={{ boxShadow: stageShadow }}
        onMouseMove={(event) => {
          pointerRef.current = { x: event.clientX, y: event.clientY };
          updateLens(event.clientX, event.clientY);
        }}
        onMouseLeave={() => {
          pointerRef.current = null;
          setLens(null);
        }}
        onScroll={() => {
          const pointer = pointerRef.current;
          if (!pointer) return;
          updateLens(pointer.x, pointer.y);
        }}
      >
        <img
          ref={imageRef}
          src={imageSrc}
          alt={item.stem}
          draggable={false}
          className={fit === 'contain' ? 'block h-full w-full select-none object-contain' : 'block select-none'}
          style={fit === 'cover' ? { minWidth: '100%', minHeight: '100%', maxWidth: 'none' } : undefined}
          onClick={onAdd}
          onContextMenu={(event) => {
            event.preventDefault();
            onDelete();
          }}
          onLoad={() => {
            if (fitRef.current !== 'cover' || !centerOnLoad.current) return;
            const stage = stageRef.current;
            if (!stage) return;
            stage.scrollLeft = (stage.scrollWidth - stage.clientWidth) / 2;
            stage.scrollTop = (stage.scrollHeight - stage.clientHeight) / 2;
            centerOnLoad.current = false;
          }}
        />
        {lens ? (
          <div
            className="pointer-events-none fixed z-40 overflow-hidden rounded-full border-2"
            style={{
              left: lens.left,
              top: lens.top,
              width: LENS_SIZE,
              height: LENS_SIZE,
              borderColor: frame ?? '#8a8a8a',
              boxShadow: ok
                ? '0 0 16px rgba(74,222,128,0.6)'
                : bad
                  ? '0 0 16px rgba(233,69,96,0.6)'
                  : '0 0 14px rgba(255,255,255,0.15)',
              backgroundImage: `url("${imageSrc}")`,
              backgroundRepeat: 'no-repeat',
              backgroundSize: lens.backgroundSize,
              backgroundPosition: lens.backgroundPosition,
              imageRendering: 'pixelated',
            }}
          />
        ) : null}
      </div>

      {stripOpen ? <Filmstrip items={items} active={index} onSelect={onSelectIndex} /> : null}

      <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border bg-[#16213e] px-3 py-1.5 text-xs text-muted-foreground">
        <button
          type="button"
          className="rounded border border-border px-2 py-0.5 hover:border-[#4a6a9a]"
          title="展开/收起预览条"
          onClick={onToggleStrip}
        >
          {index + 1}/{items.length}
        </button>
        <span className="border-l border-border pl-2">符合 {okCount}</span>
        <span className="max-w-56 truncate border-l border-border pl-2" title={item.path}>
          {item.stem}
        </span>
        <span className="border-l border-border pl-2">{statusText}</span>
        <span className="border-l border-border pl-2">
          {item.width && item.height ? `${item.width}×${item.height}` : '-'}
        </span>
        <span
          className="flex-1 truncate border-l border-border pl-2 font-mono"
          title={item.caption_preview || ''}
        >
          {item.caption_preview || '-'}
        </span>
      </div>
    </div>
  );
}
