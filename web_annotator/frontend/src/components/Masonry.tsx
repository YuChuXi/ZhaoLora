import { useEffect, useMemo, useRef, useState } from 'react';
import type { DownloadStatus } from '@/lib/types';
import { cn } from '@/lib/utils';

export interface ViewItem {
  key: string;
  ratio: number;
  src: string;
  label: string;
  kind?: 'image' | 'video';
}

interface MasonryProps {
  items: ViewItem[];
  fixedColumns: number;
  selected: Record<string, boolean>;
  status: Record<string, DownloadStatus>;
  progress: Record<string, string>;
  hasMore: boolean;
  hasPrev: boolean;
  loading: boolean;
  onCardClick: (key: string, shiftKey: boolean) => void;
  onCardContext: (key: string) => void;
  onLoadMore: (up: boolean) => void;
}

const GAP = 8;
const CARD_BORDER = 4;
const INFO_H = 28;
const MIN_COL_W = 240;

function distribute(items: ViewItem[], columns: number, width: number): ViewItem[][] {
  const colWidth = Math.max(1, (width - 16 - GAP * (columns - 1)) / columns);
  const buckets: ViewItem[][] = Array.from({ length: columns }, () => []);
  const heights = new Array(columns).fill(0);
  for (const item of items) {
    let target = 0;
    for (let i = 1; i < columns; i += 1) {
      if (heights[i] < heights[target]) target = i;
    }
    buckets[target].push(item);
    heights[target] += CARD_BORDER + Math.max(0, colWidth - CARD_BORDER) * item.ratio + INFO_H + GAP;
  }
  return buckets;
}

function FilterCard({
  item,
  selected,
  status,
  progress,
  onClick,
  onContext,
}: {
  item: ViewItem;
  selected: boolean;
  status?: DownloadStatus;
  progress?: string;
  onClick: (shiftKey: boolean) => void;
  onContext: () => void;
}) {
  const timer = useRef<number>();
  const longPressed = useRef(false);

  const cancelLongPress = () => {
    if (timer.current) {
      window.clearTimeout(timer.current);
      timer.current = undefined;
    }
  };

  return (
    <div
      className={cn('masonry-card', item.kind === 'video' && '')}
      data-key={item.key}
      data-selected={selected ? 'true' : 'false'}
      data-kind={item.kind}
      title={item.label}
      onClick={(event) => {
        if (longPressed.current) {
          longPressed.current = false;
          return;
        }
        onClick(event.shiftKey);
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
        src={item.src}
        alt={item.label}
        loading="lazy"
        decoding="async"
        style={{ aspectRatio: `1 / ${item.ratio}` }}
        draggable={false}
      />
      <div className="card-info">
        <span className="card-name">
          {item.kind === 'video' ? '视频 · ' : ''}
          {progress || item.label}
        </span>
        {status === 'downloading' ? <span className="text-[10px] text-sky-300">…</span> : null}
      </div>
      <span className="card-check">✓</span>
    </div>
  );
}

export function Masonry({
  items,
  fixedColumns,
  selected,
  status,
  progress,
  hasMore,
  hasPrev,
  loading,
  onCardClick,
  onCardContext,
  onLoadMore,
}: MasonryProps) {
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

  const columns = fixedColumns > 0 ? fixedColumns : Math.max(1, Math.floor(width / MIN_COL_W));
  const buckets = useMemo(() => distribute(items, columns, width), [items, columns, width]);

  const bottomRef = useRef<HTMLDivElement>(null);
  const topRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const target = bottomRef.current;
    if (!target || !hasMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting) && !loading) onLoadMore(false);
      },
      { rootMargin: '280px 0px' },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, loading, onLoadMore]);

  useEffect(() => {
    const target = topRef.current;
    if (!target || !hasPrev) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting) && !loading) onLoadMore(true);
      },
      { rootMargin: '280px 0px' },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasPrev, loading, onLoadMore]);

  if (!items.length) {
    return (
      <div ref={hostRef}>
        <div className="py-16 text-center text-[13px] text-muted-foreground">
          {loading ? '加载中…' : '没有图片'}
        </div>
      </div>
    );
  }

  return (
    <div ref={hostRef}>
      <div ref={topRef} className="h-px" />
      <div className="masonry-grid">
        {buckets.map((bucket, index) => (
          <div className="masonry-column" key={index}>
            {bucket.map((item) => (
              <FilterCard
                key={item.key}
                item={item}
                selected={!!selected[item.key]}
                status={status[item.key]}
                progress={progress[item.key]}
                onClick={(shiftKey) => onCardClick(item.key, shiftKey)}
                onContext={() => onCardContext(item.key)}
              />
            ))}
          </div>
        ))}
      </div>
      <div ref={bottomRef} className="h-px" />
    </div>
  );
}
