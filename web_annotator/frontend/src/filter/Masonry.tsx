import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { scrollContainer } from '@/lib/scroll';

export interface ViewItem {
  key: string;
  ratio: number;
  src: string;
  label: string;
  kind?: 'image' | 'video';
  fallbackSrc?: string;
  highlight?: boolean;
  tooltip?: string;
  /** 排序项角标: 当前排序键对应的值, 如美学分、像素或日期. */
  badge?: string;
  /** 该卡片来自的搜索结果页, 用于滚动时同步页码. */
  page?: number;
}

interface MasonryProps {
  items: ViewItem[];
  fixedColumns: number;
  selected: Record<string, boolean>;
  hasMore: boolean;
  hasPrev: boolean;
  loading: boolean;
  onCardClick: (key: string, shiftKey: boolean) => void;
  onCardContext: (key: string) => void;
  onLoadMore: (up: boolean) => void;
}

const GAP = 8;
/** 网格内边距, 与 `.masonry-grid` 的 `padding` 一致. */
const PAD = 8;
const CARD_BORDER = 4;
const INFO_H = 28;
const MIN_COL_W = 240;

/**
 * 一列: 条目键、列首顶偏移与列底偏移.
 *
 * `top` 是列首卡片相对网格内容顶的偏移, 非负; 向上加载时新卡压在 `top` 之上,
 * 该列 `top` 随之前移, 不足部分由统一的滚动补偿抹平.
 */
interface Column {
  keys: string[];
  top: number;
  bottom: number;
}

/** 列表布局: 条目顺序、各列状态、列数与容器宽度. */
interface Layout {
  order: string[];
  columns: Column[];
  count: number;
  width: number;
  /** 本次插入让所有已加载卡片下移的距离, 即向上加载的滚动补偿量. */
  shift: number;
}

/**
 * 卡片占用列高: 边框、图片、信息栏与下间距.
 *
 * Args:
 *   item (ViewItem): 卡片条目.
 *   width (number): 容器内容宽度.
 *   count (number): 列数.
 *
 * Returns:
 *   number: 卡片总高.
 *
 * Callers:
 *   - `filter/Masonry.tsx:attach`
 */
function cardHeight(item: ViewItem, width: number, count: number): number {
  const columnWidth = Math.max(1, (width - PAD * 2 - GAP * (count - 1)) / count);
  return CARD_BORDER + Math.max(0, columnWidth - CARD_BORDER) * item.ratio + INFO_H + GAP;
}

/**
 * 把一批条目接入各列, 返回每列接入的高度.
 *
 * 两个方向共用同一规则, 只有接续边不同: 向下接在列底之后, 向上接在列顶之前,
 * 每次都选最靠近接缝的列, 使该侧边缘趋于整齐.
 *
 * Args:
 *   fresh (ViewItem[]): 本批条目, 按视觉顺序排列.
 *   width (number): 容器内容宽度.
 *   count (number): 列数.
 *   columns (Column[]): 各列状态, 原位置入条目键.
 *   up (boolean): 是否插入列首.
 *
 * Returns:
 *   number[]: 各列因本批条目增加的高度.
 *
 * Callers:
 *   - `filter/Masonry.tsx:layoutAll`
 *   - `filter/Masonry.tsx:extend`
 */
function attach(
  fresh: ViewItem[],
  width: number,
  count: number,
  columns: Column[],
  up: boolean,
): number[] {
  const edge = columns.map((column) => (up ? -column.top : column.bottom));
  const gains = columns.map(() => 0);
  for (const item of up ? [...fresh].reverse() : fresh) {
    let target = 0;
    for (let index = 1; index < edge.length; index += 1) {
      if (edge[index] < edge[target]) target = index;
    }
    const height = cardHeight(item, width, count);
    if (up) columns[target].keys.unshift(item.key);
    else columns[target].keys.push(item.key);
    gains[target] += height;
    edge[target] += height;
  }
  return gains;
}

/**
 * 建立全量布局.
 *
 * Args:
 *   items (ViewItem[]): 列表条目.
 *   count (number): 列数.
 *   width (number): 容器内容宽度.
 *
 * Returns:
 *   Layout: 全量分列结果, 无留白、无滚动补偿.
 *
 * Callers:
 *   - `filter/Masonry.tsx:Masonry`
 */
function layoutAll(items: ViewItem[], count: number, width: number): Layout {
  const columns: Column[] = Array.from({ length: count }, () => ({ keys: [], top: 0, bottom: 0 }));
  const gains = attach(items, width, count, columns, false);
  for (const [index, column] of columns.entries()) column.bottom = gains[index];
  return { order: items.map((item) => item.key), columns, count, width, shift: 0 };
}

/**
 * 判断新列表相对旧列表的扩展方向.
 *
 * Args:
 *   before (string[]): 旧列表的条目键.
 *   order (string[]): 新列表的条目键.
 *
 * Returns:
 *   'up' | 'down' | null: 旧列表连续位于新列表首部之后为向上, 位于尾部为向下;
 *     旧列表被插空、删除或重排时为 null.
 *
 * Callers:
 *   - `filter/Masonry.tsx:extend`
 */
function locate(before: string[], order: string[]): 'up' | 'down' | null {
  const at = new Map<string, number>();
  order.forEach((key, index) => at.set(key, index));
  let first = -1;
  for (const [index, key] of before.entries()) {
    const position = at.get(key);
    if (position === undefined) return null;
    if (first < 0) {
      first = position;
      continue;
    }
    if (position !== first + index) return null;
  }
  if (first === 0) return 'down';
  if (first === order.length - before.length) return 'up';
  return null;
}

/**
 * 在既有布局上扩展一页.
 *
 * 条目键未变时直接沿用既有布局, 下载状态等字段变化不会触发重排.
 *
 * 向下加载是纯追加: 列顶与滚动位置都不动.
 *
 * 向上加载插入列首; 各列新增高度不同, 已加载卡片要留在原视口位置就要求所有列
 * 内容下移同一个距离 `shift`. 新卡占据列顶之上, 故该列列顶须满足
 * `shift >= gain - top`, 取各列此值的最小可行解: 列顶富余的列先被新卡抵掉,
 * 只有全部列都不足时才整体抬高. 抬高量在绘制前一次性滚动抵消.
 *
 * Args:
 *   previous (Layout): 当前布局.
 *   items (ViewItem[]): 更新后的列表.
 *   count (number): 列数.
 *   width (number): 容器内容宽度.
 *
 * Returns:
 *   Layout | null: 扩展后的布局; 列表被整体替换或重排时为 null.
 *
 * Callers:
 *   - `filter/Masonry.tsx:Masonry`
 */
function extend(
  previous: Layout,
  items: ViewItem[],
  count: number,
  width: number,
): Layout | null {
  const order = items.map((item) => item.key);
  const before = previous.order;
  if (!before.length) return null;
  if (order.length === before.length) {
    return before.every((key, index) => order[index] === key) ? previous : null;
  }
  if (order.length < before.length) return null;
  const direction = locate(before, order);
  if (!direction) return null;
  const up = direction === 'up';
  const fresh = up ? items.slice(0, items.length - before.length) : items.slice(before.length);
  const columns = previous.columns.map((column) => ({ ...column, keys: [...column.keys] }));
  const gains = attach(fresh, width, count, columns, up);
  if (!up) {
    for (const [index, column] of columns.entries()) {
      column.bottom = previous.columns[index].bottom + gains[index];
    }
    return { order, columns, count, width, shift: 0 };
  }
  let shift = 0;
  for (const [index, column] of columns.entries()) {
    shift = Math.max(shift, gains[index] - column.top);
  }
  for (const [index, column] of columns.entries()) {
    column.top += shift - gains[index];
    column.bottom = previous.columns[index].bottom + shift;
  }
  return { order, columns, count, width, shift };
}

/**
 * 一张瀑布流卡片.
 *
 * Args:
 *   item (ViewItem): 卡片数据.
 *   selected (boolean): 是否选中.
 *   onCardClick (function): 点击回调.
 *   onCardContext (function): 预览回调.
 *
 * Returns:
 *   JSX.Element: 卡片节点.
 *
 * Callers:
 *   - `filter/Masonry.tsx:Masonry`
 */
const FilterCard = memo(function FilterCard({
  item,
  selected,
  onCardClick,
  onCardContext,
}: {
  item: ViewItem;
  selected: boolean;
  onCardClick: (key: string, shiftKey: boolean) => void;
  onCardContext: (key: string) => void;
}) {
  const timer = useRef<number>();
  const longPressed = useRef(false);

  /**
   * 取消长按计时.
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `filter/Masonry.tsx:FilterCard` 指针事件.
   */
  const cancelLongPress = () => {
    window.clearTimeout(timer.current);
    timer.current = undefined;
  };

  return (
    <div
      className={cn('masonry-card', item.kind === 'video' && '')}
      data-key={item.key}
      data-page={item.page}
      data-selected={selected ? 'true' : 'false'}
      data-kind={item.kind}
      data-highlight={item.highlight ? 'true' : 'false'}
      title={item.tooltip || item.label}
      onClick={(event) => {
        if (longPressed.current) {
          longPressed.current = false;
          return;
        }
        onCardClick(item.key, event.shiftKey);
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        onCardContext(item.key);
      }}
      onPointerDown={(event) => {
        if (event.pointerType !== 'touch') return;
        timer.current = window.setTimeout(() => {
          longPressed.current = true;
          onCardContext(item.key);
        }, 550);
      }}
      onPointerUp={cancelLongPress}
      onPointerCancel={cancelLongPress}
      onPointerMove={cancelLongPress}
    >
      <img
        src={item.src}
        alt={item.label}
        loading="lazy"
        decoding="async"
        style={{ aspectRatio: `1 / ${item.ratio}` }}
        draggable={false}
        onError={(event) => {
          const image = event.currentTarget;
          if (!item.fallbackSrc || image.dataset.fallback === '1') return;
          image.dataset.fallback = '1';
          image.src = item.fallbackSrc;
        }}
      />
      <div className="card-info">
        <span className="card-name">
          {item.kind === 'video' ? '视频 · ' : ''}
          {item.label}
        </span>
      </div>
      {item.badge ? <span className="card-badge">{item.badge}</span> : null}
      <span className="card-check">✓</span>
    </div>
  );
});

/**
 * 首尾增量瀑布流: 已加载卡片的视口位置在加载前后完全不变.
 *
 * Args:
 *   props (MasonryProps): 条目、列数、加载边界与交互回调.
 *
 * Returns:
 *   JSX.Element: 分列卡片与首尾加载哨兵.
 *
 * Callers:
 *   - `filter/FilterPage.tsx:FilterPage`
 *   - `pages/scoring/ScoringPage.tsx:ScoringPage`
 *   - `pages/caption/CaptionPage.tsx:CaptionPage`
 */
export function Masonry({
  items,
  fixedColumns,
  selected,
  hasMore,
  hasPrev,
  loading,
  onCardClick,
  onCardContext,
  onLoadMore,
}: MasonryProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const previous = useRef<Layout | null>(null);
  const [width, setWidth] = useState(0);

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    setWidth(host.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      setWidth(entries[0].contentRect.width);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  const byKey = useMemo(() => new Map(items.map((item) => [item.key, item])), [items]);
  const layout = useMemo<Layout | null>(() => {
    if (width <= 0) return null;
    const count = fixedColumns > 0 ? fixedColumns : Math.max(1, Math.floor(width / MIN_COL_W));
    const current = previous.current;
    if (current && current.count === count && current.width === width) {
      const extended = extend(current, items, count, width);
      if (extended) return extended;
    }
    return layoutAll(items, count, width);
  }, [fixedColumns, items, width]);

  useLayoutEffect(() => {
    if (!layout) return;
    if (layout.shift > 0) scrollContainer().scrollTop += layout.shift;
    previous.current = layout;
  }, [layout]);

  const bottomRef = useRef<HTMLDivElement>(null);
  const topRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const top = topRef.current;
    const bottom = bottomRef.current;
    if (!top || !bottom || loading) return;
    const observer = new IntersectionObserver(
      (entries) => {
        let up = false;
        let down = false;
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          if (entry.target === top) up = true;
          else if (entry.target === bottom) down = true;
        }
        if (up && hasPrev) onLoadMore(true);
        else if (down && hasMore) onLoadMore(false);
      },
      { rootMargin: '280px 0px' },
    );
    observer.observe(top);
    observer.observe(bottom);
    return () => observer.disconnect();
  }, [hasMore, hasPrev, loading, onLoadMore]);

  if (!items.length) {
    return (
      <div ref={hostRef} className="masonry-host">
        <div className="py-16 text-center text-sm text-muted-foreground">
          {loading ? '加载中…' : '没有图片'}
        </div>
      </div>
    );
  }

  const columns = layout?.columns ?? [];
  const sentinelTop = columns.length ? Math.max(...columns.map((column) => column.top)) : 0;
  const sentinelBottom = columns.length ? Math.min(...columns.map((column) => column.bottom)) : 0;

  return (
    <div ref={hostRef} className="masonry-host">
      <div className="masonry-grid">
        <div ref={topRef} className="masonry-sentinel" style={{ top: PAD + sentinelTop }} />
        {columns.map((column, index) => (
          <div
            className="masonry-column"
            key={index}
            style={{ paddingTop: column.top }}
          >
            {column.keys.map((key) => (
              <FilterCard
                key={key}
                item={byKey.get(key) as ViewItem}
                selected={!!selected[key]}
                onCardClick={onCardClick}
                onCardContext={onCardContext}
              />
            ))}
          </div>
        ))}
        <div ref={bottomRef} className="masonry-sentinel" style={{ top: PAD + sentinelBottom }} />
      </div>
    </div>
  );
}
