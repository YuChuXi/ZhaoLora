import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Input } from './input';
import { cn } from '@/lib/utils';

/** 一条补全候选. */
export interface TagSuggestion {
  name: string;
  /** 候选的类型说明, 如标签分类或 WD14 类别. */
  note?: string;
  /** 候选的使用次数. */
  count?: number;
}

/** 下拉列表的固定定位框; 挂到 body 上避免被工具栏/对话框的滚动容器裁剪. */
interface DropdownBox {
  left: number;
  width: number;
  top?: number;
  bottom?: number;
  maxHeight: number;
}

/**
 * 把使用次数压缩为 K/M 单位文本.
 *
 * Args:
 *   value (number): 原始次数.
 *
 * Returns:
 *   string: 千位以上保留一位小数并去掉多余的 `.0`, 如 `45923` → `45.9K`.
 *
 * Callers:
 *   - `components/ui/tag-suggest.tsx:TagSuggestInput`
 */
function compactCount(value: number): string {
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(1).replace(/\.0$/, '')}K`;
  }
  return String(value);
}

/**
 * 带补全下拉的标签输入框.
 *
 * 候选取输入框中最后一个片段 (按分隔符切分), 选中后替换该片段并补上分隔符,
 * 保留前面已输入的标签; 上下方向键移动高亮, Enter 选中, Esc 关闭。
 *
 * Args:
 *   value (string): 输入框文本.
 *   onChange (function): 文本变化回调.
 *   fetchTags (function): 按片段取候选, 支持中止.
 *   separator (' ' | ','): 标签分隔符.
 *   placeholder (string): 输入提示.
 *   className (string): 容器布局类; 输入框始终填满容器.
 *   onSubmit (function): 无高亮候选时按 Enter 的回调.
 *
 * Returns:
 *   JSX.Element: 输入框与补全列表.
 *
 * Callers:
 *   - `filter/Toolbar.tsx:SearchSection`
 *   - `filter/Dialogs.tsx:SearchDialog`
 */
export function TagSuggestInput({
  value,
  onChange,
  fetchTags,
  separator = ' ',
  placeholder,
  className,
  onSubmit,
}: {
  value: string;
  onChange: (value: string) => void;
  fetchTags: (query: string, signal: AbortSignal) => Promise<TagSuggestion[]>;
  separator?: ' ' | ',';
  placeholder?: string;
  className?: string;
  onSubmit?: () => void;
}) {
  const [options, setOptions] = useState<TagSuggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [box, setBox] = useState<DropdownBox | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const timer = useRef<number>();
  const closeTimer = useRef<number>();

  const trailing = separator === ',' ? ', ' : ' ';
  const pattern = separator === ',' ? /[^,]*$/ : /[^\s,]*$/;
  const rawFragment = value.match(pattern)?.[0] ?? '';
  // 排除前缀 (`-tag`) 属于查询语法, 补全查询只取标签本身.
  const fragment = rawFragment.trim().replace(/^-+/, '');

  useEffect(() => {
    window.clearTimeout(timer.current);
    if (!fragment) {
      setOptions([]);
      return;
    }
    const controller = new AbortController();
    timer.current = window.setTimeout(() => {
      fetchTags(fragment, controller.signal)
        .then((items) => {
          const used = new Set(
            value
              .split(separator === ',' ? ',' : /[\s,]+/)
              .map((item) => item.trim())
              .filter(Boolean),
          );
          setOptions(items.filter((item) => !used.has(item.name)));
          setActive(-1);
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === 'AbortError') return;
          setOptions([]);
        });
    }, 250);
    return () => {
      window.clearTimeout(timer.current);
      controller.abort();
    };
  }, [fragment, value, separator, fetchTags]);

  const pick = (item: TagSuggestion) => {
    const operator = /^-+/.exec(rawFragment.trim())?.[0] ?? '';
    const head = value.slice(0, value.length - rawFragment.length).trimEnd();
    onChange(`${head ? `${head}${trailing}` : ''}${operator}${item.name}${trailing}`);
    setOptions([]);
    setOpen(false);
    setActive(-1);
  };

  /**
   * 依据输入框位置刷新下拉框的固定定位.
   *
   * 下方空间不足时向上展开, 高度取可用空间与上限的较小值; 滚动与窗口变化时同步.
   *
   * Callers:
   *   - `components/ui/tag-suggest.tsx:TagSuggestInput` 展开下拉与滚动监听.
   */
  const placeDropdown = useCallback(() => {
    const field = wrapRef.current?.querySelector('input');
    if (!field) return;
    const rect = field.getBoundingClientRect();
    const below = window.innerHeight - rect.bottom - 8;
    const above = rect.top - 8;
    const openUp = below < 160 && above > below;
    const maxHeight = Math.max(96, Math.min(224, openUp ? above : below));
    setBox({
      left: rect.left,
      width: rect.width,
      maxHeight,
      ...(openUp ? { bottom: window.innerHeight - rect.top + 4 } : { top: rect.bottom + 4 }),
    });
  }, []);

  useEffect(() => {
    if (!open || !options.length) {
      setBox(null);
      return;
    }
    placeDropdown();
    window.addEventListener('resize', placeDropdown);
    window.addEventListener('scroll', placeDropdown, true);
    return () => {
      window.removeEventListener('resize', placeDropdown);
      window.removeEventListener('scroll', placeDropdown, true);
    };
  }, [open, options.length, placeDropdown]);

  return (
    <div ref={wrapRef} className={cn('relative min-w-0', className)}>
      <Input
        className="w-full min-w-0"
        value={value}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        onFocus={() => setOpen(true)}
        onBlur={() => {
          closeTimer.current = window.setTimeout(() => setOpen(false), 120);
        }}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            setOpen(false);
            return;
          }
          if (event.key === 'Enter' && (active < 0 || !options.length)) {
            if (!onSubmit) return;
            event.preventDefault();
            onSubmit();
            return;
          }
          if (!open || !options.length) return;
          if (event.key === 'ArrowDown') {
            event.preventDefault();
            setActive((current) => (current + 1) % options.length);
          } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            setActive((current) => (current <= 0 ? options.length - 1 : current - 1));
          } else if (event.key === 'Enter' && active >= 0) {
            event.preventDefault();
            window.clearTimeout(closeTimer.current);
            pick(options[active]);
          }
        }}
      />
      {open && options.length && box
        ? createPortal(
            <div
              className="fixed z-[80] overflow-auto rounded-md border border-border bg-popover py-1 text-popover-foreground shadow-lg"
              style={{
                left: box.left,
                minWidth: box.width,
                width: 'max-content',
                maxWidth: 'calc(100vw - 2rem)',
                maxHeight: box.maxHeight,
                ...(box.top !== undefined ? { top: box.top } : { bottom: box.bottom }),
              }}
            >
              {options.map((item, index) => (
                <button
                  key={item.name}
                  type="button"
                  className={cn(
                    'flex w-full items-center justify-between gap-3 px-2 py-1 text-left text-xs text-foreground',
                    index === active ? 'bg-accent' : 'hover:bg-accent',
                  )}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    window.clearTimeout(closeTimer.current);
                    pick(item);
                  }}
                >
                  <span className="min-w-0 flex-1 truncate">{item.name}</span>
                  <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                    {item.note ? <span>{item.note}</span> : null}
                    {item.count !== undefined && item.count > 0 ? (
                      <span className="tabular-nums" title={item.count.toLocaleString()}>
                        {compactCount(item.count)}
                      </span>
                    ) : null}
                  </span>
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
