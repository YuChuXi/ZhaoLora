import { useEffect, useRef } from 'react';
import { cn } from '@/lib/utils';
import { thumbnailSource, type TagCheckItem } from './model';

interface FilmstripProps {
  items: TagCheckItem[];
  active: number;
  onSelect: (index: number) => void;
}

/**
 * 横排缩略图预览条：展示当前结果列表，点击缩略图跳转到对应图片。
 *
 * Args:
 *   items (TagCheckItem[]): 当前检查结果列表。
 *   active (number): 当前单图索引。
 *   onSelect (function): 点击缩略图的跳转回调。
 *
 * Returns:
 *   JSX.Element: 横向滚动缩略图条。
 *
 * Callers:
 *   - `pages/tag-check/SingleView.tsx:SingleView`
 */
export function Filmstrip({ items, active, onSelect }: FilmstripProps) {
  const activeRef = useRef<HTMLImageElement>(null);

  useEffect(() => {
    activeRef.current?.scrollIntoView({ inline: 'center', block: 'nearest' });
  }, [active]);

  return (
    <div className="flex h-[152px] shrink-0 items-center gap-1.5 overflow-x-auto border-t border-border bg-[#16213e] px-3">
      {items.map((item, index) => (
        <img
          key={item.path}
          ref={index === active ? activeRef : undefined}
          src={thumbnailSource(item)}
          alt={item.stem}
          title={item.stem}
          loading="lazy"
          draggable={false}
          className={cn(
            'h-[120px] w-auto shrink-0 cursor-pointer rounded-sm border-2 bg-[#0a1a30]',
            index === active ? 'border-[#4ade80]' : 'border-transparent hover:border-muted-foreground',
          )}
          style={item.width > 0 && item.height > 0 ? { aspectRatio: `${item.width} / ${item.height}` } : undefined}
          onClick={() => onSelect(index)}
        />
      ))}
    </div>
  );
}
