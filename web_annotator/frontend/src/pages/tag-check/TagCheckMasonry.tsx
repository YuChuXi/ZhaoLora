import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { imageSource, type TagCheckItem } from './model';

interface TagCheckMasonryProps {
  items: TagCheckItem[];
  columns: number;
  busy: Record<string, boolean>;
  flash: Record<string, boolean>;
  applyMask: boolean;
  onCardClick: (path: string) => void;
  onCardContext: (path: string) => void;
}

const GAP = 8;
const CARD_BORDER = 4;
const INFO_H = 28;
const MIN_COL_W = 240;

/**
 * 估算图片纵向比例，尺寸缺失时按正方形处理。
 *
 * Args:
 *   item (TagCheckItem): 图片状态项。
 *
 * Returns:
 *   number: 高宽比。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckMasonry.tsx:distribute`
 */
function ratioOf(item: TagCheckItem): number {
  return item.width > 0 && item.height > 0 ? item.height / item.width : 1;
}

/**
 * 按最短列优先把图片分配到各列。
 *
 * Args:
 *   items (TagCheckItem[]): 待布局图片。
 *   columns (number): 列数。
 *   width (number): 容器宽度。
 *
 * Returns:
 *   TagCheckItem[][]: 每列的图片列表。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckMasonry.tsx:TagCheckMasonry`
 */
function distribute(items: TagCheckItem[], columns: number, width: number): TagCheckItem[][] {
  const columnWidth = Math.max(1, (width - 16 - GAP * (columns - 1)) / columns);
  const buckets: TagCheckItem[][] = Array.from({ length: columns }, () => []);
  const heights = new Array(columns).fill(0);
  for (const item of items) {
    let target = 0;
    for (let index = 1; index < columns; index += 1) {
      if (heights[index] < heights[target]) target = index;
    }
    buckets[target].push(item);
    heights[target] +=
      CARD_BORDER + Math.max(0, columnWidth - CARD_BORDER) * ratioOf(item) + INFO_H + GAP;
  }
  return buckets;
}

/**
 * 生成标签状态对应的边框颜色与光晕。
 *
 * Args:
 *   item (TagCheckItem): 图片状态项。
 *
 * Returns:
 *   { border: string | undefined; shadow: string | undefined }: 内联样式值。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckMasonry.tsx:TagCheckCard`
 */
function stateStyle(item: TagCheckItem): { border?: string; shadow?: string } {
  if (item.exists && item.position_ok) {
    return {
      border: '#4ade80',
      shadow: '0 0 0 1px #4ade80, 0 2px 12px rgba(74,222,128,0.25)',
    };
  }
  if (item.exists) {
    return {
      border: '#e94560',
      shadow: '0 0 0 1px #e94560, 0 2px 12px rgba(233,69,96,0.25)',
    };
  }
  return {};
}

/**
 * 生成卡片信息栏文本：相似度 / 掩码占比前缀 + 文件名。
 *
 * Args:
 *   item (TagCheckItem): 图片状态项。
 *
 * Returns:
 *   string: 信息栏文本。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckMasonry.tsx:TagCheckCard`
 */
function cardText(item: TagCheckItem): string {
  let prefix = '';
  if (item.sim != null) prefix += `S${item.sim.toFixed(3)} `;
  if (item.ratio != null) prefix += `R${item.ratio.toFixed(3)} `;
  return `${prefix}${item.stem}`;
}

/**
 * 单张检查结果卡片：按标签状态着色，承载蒙板叠加与长按/右键进入单图。
 *
 * Args:
 *   item (TagCheckItem): 图片状态项。
 *   busy (boolean): 是否正在写入标签。
 *   flash (boolean): 是否显示写入成功的高亮反馈。
 *   applyMask (boolean): 是否显示蒙板合成图。
 *   onClick (function): 单击回调。
 *   onContext (function): 右键或长按回调。
 *
 * Returns:
 *   JSX.Element: 卡片节点。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckMasonry.tsx:TagCheckMasonry`
 */
const TagCheckCard = memo(function TagCheckCard({
  item,
  busy,
  flash,
  applyMask,
  onClick,
  onContext,
}: {
  item: TagCheckItem;
  busy: boolean;
  flash: boolean;
  applyMask: boolean;
  onClick: () => void;
  onContext: () => void;
}) {
  const timer = useRef<number>();
  const longPressed = useRef(false);
  const style = stateStyle(item);

  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current);
    },
    [],
  );

  /**
   * 取消触屏长按计时器。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagCheckMasonry.tsx:TagCheckCard`
   */
  const cancelLongPress = () => {
    if (timer.current) {
      window.clearTimeout(timer.current);
      timer.current = undefined;
    }
  };

  return (
    <div
      className={cn('masonry-card', busy && 'pointer-events-none opacity-50')}
      data-key={item.path}
      data-flash={flash ? 'true' : 'false'}
      title={item.caption_preview || '(无 .txt)'}
      style={{
        borderColor: style.border,
        boxShadow: style.shadow,
      }}
      onClick={() => {
        if (longPressed.current) {
          longPressed.current = false;
          return;
        }
        onClick();
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        onContext();
      }}
      onPointerDown={(event) => {
        if (event.pointerType !== 'touch') return;
        timer.current = window.setTimeout(() => {
          longPressed.current = true;
          onContext();
        }, 550);
      }}
      onPointerUp={cancelLongPress}
      onPointerCancel={cancelLongPress}
      onPointerMove={cancelLongPress}
    >
      <img
        src={imageSource(item, applyMask)}
        alt={item.stem}
        loading="lazy"
        decoding="async"
        draggable={false}
        style={{ aspectRatio: `1 / ${ratioOf(item)}` }}
      />
      {item.mask ? (
        <img
          className="pointer-events-none absolute left-0 top-0 z-[2] w-full"
          src={item.mask}
          alt=""
          loading="lazy"
          style={{ background: 'transparent' }}
        />
      ) : null}
      <div className="card-info">
        <span className="card-name">{cardText(item)}</span>
      </div>
    </div>
  );
});

/**
 * 标签检查结果瀑布流：按检查结果着色，支持单击增删标签与右键/长按进入单图。
 *
 * Args:
 *   items (TagCheckItem[]): 检查结果列表。
 *   columns (number): 固定列数，0 表示按容器宽度自动计算。
 *   busy (Record<string, boolean>): 写入中的图片路径映射。
 *   flash (Record<string, boolean>): 写入成功高亮反馈的路径映射。
 *   applyMask (boolean): 是否显示蒙板合成图。
 *   onCardClick (function): 单击回调，参数为路径。
 *   onCardContext (function): 右键或长按回调，参数为路径。
 *
 * Returns:
 *   JSX.Element: 瀑布流节点。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 */
export function TagCheckMasonry({
  items,
  columns,
  busy,
  flash,
  applyMask,
  onCardClick,
  onCardContext,
}: TagCheckMasonryProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    setWidth(host.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      setWidth(entries[0].contentRect.width);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  const effectiveColumns = columns > 0 ? columns : Math.max(1, Math.floor(width / MIN_COL_W));
  const buckets = useMemo(
    () => distribute(items, effectiveColumns, width),
    [items, effectiveColumns, width],
  );

  return (
    <div ref={hostRef}>
      {items.length ? (
        <div className="masonry-grid">
          {buckets.map((bucket, index) => (
            <div className="masonry-column" key={index}>
              {bucket.map((item) => (
                <TagCheckCard
                  key={item.path}
                  item={item}
                  busy={!!busy[item.path]}
                  flash={!!flash[item.path]}
                  applyMask={applyMask}
                  onClick={() => onCardClick(item.path)}
                  onContext={() => onCardContext(item.path)}
                />
              ))}
            </div>
          ))}
        </div>
      ) : (
        <div className="py-16 text-center text-[13px] text-muted-foreground">没有图片</div>
      )}
    </div>
  );
}
