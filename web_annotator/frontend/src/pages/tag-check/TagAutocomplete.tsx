import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { matchTags, normalizeTag, type TagStatRow } from './model';

interface TagAutocompleteProps {
  value: string;
  tags: TagStatRow[];
  placeholder?: string;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
}

const LIMIT = 60;

/**
 * 渲染补全候选中的匹配子串。
 *
 * Args:
 *   tag (string): 候选标签。
 *   query (string): 当前输入。
 *
 * Returns:
 *   JSX.Element | string: 带高亮文本的节点或原始标签。
 *
 * Callers:
 *   - `pages/tag-check/TagAutocomplete.tsx:TagAutocomplete`
 */
function highlightTag(tag: string, query: string) {
  const normalized = normalizeTag(query.trim());
  if (!normalized) return tag;
  const index = normalizeTag(tag).indexOf(normalized);
  if (index < 0) return tag;
  return (
    <>
      {tag.slice(0, index)}
      <span className="font-semibold text-[#4ade80]">{tag.slice(index, index + normalized.length)}</span>
      {tag.slice(index + normalized.length)}
    </>
  );
}

/**
 * 标签输入框与本地补全下拉，行为与原标签检查页一致（防抖、键盘导航、Enter 触发检查）。
 *
 * Args:
 *   value (string): 当前输入值。
 *   tags (TagStatRow[]): 目录标签统计，作为补全数据源。
 *   placeholder (string): 输入提示。
 *   onChange (function): 输入变化回调。
 *   onSubmit (function): Enter 确认回调，参数为本次提交使用的标签值。
 *
 * Returns:
 *   JSX.Element: 输入框与下拉列表。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 */
export function TagAutocomplete({ value, tags, placeholder, onChange, onSubmit }: TagAutocompleteProps) {
  const [query, setQuery] = useState(value);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const blurTimer = useRef<number>();
  const itemRefs = useRef<Array<HTMLDivElement | null>>([]);

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(value), 120);
    return () => window.clearTimeout(timer);
  }, [value]);

  const matches = useMemo(() => matchTags(tags, query, LIMIT), [tags, query]);

  useEffect(() => {
    setActive(0);
  }, [matches]);

  useEffect(() => {
    if (!tags.length) setOpen(false);
  }, [tags]);

  useEffect(() => {
    if (!open) return;
    itemRefs.current[active]?.scrollIntoView({ block: 'nearest' });
  }, [active, open, matches]);

  /**
   * 选中候选：写回输入框、关闭下拉并恢复焦点。
   *
   * Args:
   *   tag (string): 选中标签。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagAutocomplete.tsx:TagAutocomplete`
   */
  const select = (tag: string) => {
    onChange(tag);
    setQuery(tag);
    setOpen(false);
    inputRef.current?.focus();
  };

  /**
   * 处理输入框键盘事件：上下移动、Esc 关闭、Tab/Enter 选中、Enter 提交。
   *
   * Args:
   *   event (KeyboardEvent<HTMLInputElement>): 键盘事件。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagAutocomplete.tsx:TagAutocomplete`
   */
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    const current = matches[active];
    if (event.key === 'ArrowDown') {
      if (!open || !matches.length) return;
      event.preventDefault();
      setActive((index) => (index + 1) % matches.length);
    } else if (event.key === 'ArrowUp') {
      if (!open || !matches.length) return;
      event.preventDefault();
      setActive((index) => (index - 1 + matches.length) % matches.length);
    } else if (event.key === 'Escape') {
      if (!open) return;
      event.preventDefault();
      setOpen(false);
    } else if (event.key === 'Tab') {
      if (!open || !current) return;
      event.preventDefault();
      select(current.tag);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const typed = inputRef.current?.value ?? value;
      const chosen = open && current ? current.tag : typed;
      if (open && current) select(current.tag);
      setOpen(false);
      onSubmit(chosen);
    }
  };

  return (
    <div className="relative inline-block">
      <Input
        ref={inputRef}
        type="search"
        className="w-[min(260px,30vw)]"
        spellCheck={false}
        autoComplete="off"
        placeholder={placeholder}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
        }}
        onFocus={() => {
          window.clearTimeout(blurTimer.current);
        }}
        onBlur={() => {
          window.clearTimeout(blurTimer.current);
          blurTimer.current = window.setTimeout(() => setOpen(false), 150);
        }}
        onKeyDown={onKeyDown}
      />
      {open && tags.length ? (
        <div className="absolute left-0 top-full z-[200] mt-0.5 max-h-[340px] min-w-full max-w-[380px] overflow-y-auto rounded border border-border bg-[#0d1b2e] text-xs shadow-[0_6px_20px_rgba(0,0,0,0.5)]">
          {matches.length ? (
            matches.map((item, index) => (
              <div
                key={item.tag}
                ref={(node) => {
                  itemRefs.current[index] = node;
                }}
                className={cn(
                  'flex cursor-pointer items-center gap-2 border-b border-white/5 px-2.5 py-1.5 leading-tight last:border-b-0 hover:bg-[#4ade80]/10',
                  index === active && 'bg-[#4ade80]/20',
                )}
                onMouseDown={(event) => {
                  event.preventDefault();
                  select(item.tag);
                }}
              >
                <span className="flex-auto text-foreground">{highlightTag(item.tag, query)}</span>
                {item.zh ? (
                  <span className="max-w-[130px] overflow-hidden text-ellipsis whitespace-nowrap text-[11px] text-muted-foreground">
                    {item.zh}
                  </span>
                ) : null}
                <span className="ml-auto whitespace-nowrap text-[11px] text-muted-foreground">
                  {item.count}
                </span>
              </div>
            ))
          ) : (
            <div className="px-2.5 py-2 text-muted-foreground">无匹配标签</div>
          )}
        </div>
      ) : null}
    </div>
  );
}
