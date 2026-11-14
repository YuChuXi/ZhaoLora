import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { api, SOURCES } from '@/lib/api';
import type { Mode, SortKey } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Checkbox, Slider } from '@/components/ui/controls';
import type { FilterActions } from './useFilterPage';
import type { PageState } from './state';

const MODES: Mode[] = [
  'local',
  'gelbooru',
  'danbooru',
  'e621',
  'bilibili',
  'bilibili-video',
  'x',
  'pixiv',
  'mihuashi',
  'video',
];

const SORTS: Array<{ value: SortKey; label: string }> = [
  { value: 'name', label: '名称' },
  { value: 'posted', label: '时间' },
  { value: 'aesthetic', label: '美学' },
  { value: 'pixels', label: '像素' },
];

function TagInput({
  source,
  value,
  onChange,
  placeholder,
}: {
  source: Mode;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  const [options, setOptions] = useState<string[]>([]);
  const timer = useRef<number>();
  const listId = useMemo(() => `tag-${Math.random().toString(36).slice(2)}`, []);

  useEffect(() => {
    window.clearTimeout(timer.current);
    const query = value.split(/[\s,]+/).pop()?.trim() ?? '';
    if (!query) {
      setOptions([]);
      return;
    }
    const controller = new AbortController();
    timer.current = window.setTimeout(() => {
      api
        .tagSuggest(source, query, controller.signal)
        .then((result) => setOptions(result.tags))
        .catch(() => setOptions([]));
    }, 250);
    return () => {
      window.clearTimeout(timer.current);
      controller.abort();
    };
  }, [value, source]);

  return (
    <>
      <Input
        className="w-[min(220px,28vw)]"
        value={value}
        placeholder={placeholder}
        list={listId}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            onChange((event.target as HTMLInputElement).value);
          }
        }}
      />
      <datalist id={listId}>
        {options.map((tag) => (
          <option key={tag} value={tag} />
        ))}
      </datalist>
    </>
  );
}

export function Toolbar({
  state,
  actions,
  selectedCount,
}: {
  state: PageState;
  actions: FilterActions;
  selectedCount: number;
}) {
  const def = SOURCES[state.mode];
  const remote = state.mode !== 'local';
  const fields = state.remote.fields;
  const anchor = useRef<{ key: string; top: number } | null>(null);

  const changeColumns = (value: number) => {
    for (const card of document.querySelectorAll<HTMLElement>('.masonry-card')) {
      const rect = card.getBoundingClientRect();
      if (rect.bottom > 0 && card.dataset.key) {
        anchor.current = { key: card.dataset.key, top: rect.top };
        break;
      }
    }
    actions.setColumns(value);
  };

  useLayoutEffect(() => {
    const saved = anchor.current;
    if (!saved) return;
    const card = document.querySelector<HTMLElement>(
      `.masonry-card[data-key="${CSS.escape(saved.key)}"]`,
    );
    if (!card) return;
    const top = card.getBoundingClientRect().top;
    if (top !== saved.top) window.scrollBy(0, top - saved.top);
  }, [state.columns]);

  return (
    <div className="sticky top-[41px] z-30 flex flex-wrap items-center gap-2.5 border-b border-border bg-background/95 px-2 py-2 backdrop-blur">
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        来源
        <Select value={state.mode} onChange={(event) => actions.setMode(event.target.value as Mode)}>
          {MODES.map((mode) => (
            <option key={mode} value={mode}>
              {SOURCES[mode].label}
            </option>
          ))}
        </Select>
      </label>

      {remote ? (
        <>
          {def.fields.map((field) => {
            if (field.type === 'text') {
              if (!field.id) return null;
              if (field.id === 'tags') {
                return (
                  <TagInput
                    key={field.id}
                    source={state.mode}
                    value={fields.tags ?? ''}
                    placeholder={field.placeholder}
                    onChange={(value) => actions.setField('tags', value)}
                  />
                );
              }
              return (
                <Input
                  key={field.id}
                  value={fields[field.id] ?? ''}
                  placeholder={field.placeholder}
                  onChange={(event) => actions.setField(field.id!, event.target.value)}
                />
              );
            }
            if (field.type === 'select' && field.id) {
              return (
                <label key={field.id} className="flex items-center gap-1 text-xs text-muted-foreground">
                  {field.label}
                  <Select
                    value={fields[field.id] ?? field.options?.[0]?.value ?? ''}
                    onChange={(event) => actions.setField(field.id!, event.target.value)}
                  >
                    {field.options?.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </label>
              );
            }
            if (field.type === 'button') {
              const onClick =
                field.role === 'search'
                  ? actions.search
                  : field.role === 'batch'
                    ? actions.openBatch
                    : actions.openMeta;
              return (
                <Button
                  key={field.role}
                  variant={field.role === 'search' ? 'primary' : 'default'}
                  onClick={onClick}
                >
                  {field.label}
                </Button>
              );
            }
            return null;
          })}

          {!def.noJump ? (
            <div className="flex h-7 items-stretch overflow-hidden rounded border border-border bg-[#0a1a30]">
              <button
                type="button"
                className="w-6 text-sm text-muted-foreground disabled:opacity-30"
                disabled={!state.remote.hasPrev || state.remote.loading}
                onClick={() => actions.loadMore(true)}
              >
                ‹
              </button>
              <span className="flex w-12 items-center justify-center border-x border-border text-xs">
                {state.remote.pageHi}
              </span>
              <button
                type="button"
                className="w-6 text-sm text-muted-foreground disabled:opacity-30"
                disabled={!state.remote.hasMore || state.remote.loading}
                onClick={() => actions.loadMore(false)}
              >
                ›
              </button>
            </div>
          ) : null}

          <span className="ml-auto text-xs text-muted-foreground">
            已加载 <b className="text-foreground">{state.remote.posts.length}</b>
          </span>
          <span className="text-xs text-muted-foreground">
            已选 <b className="text-foreground">{selectedCount}</b>
          </span>
        </>
      ) : (
        <>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            排序
            <Select value={state.sort} onChange={(event) => actions.setSort(event.target.value as SortKey)}>
              {SORTS.map((sort) => (
                <option key={sort.value} value={sort.value}>
                  {sort.label}
                </option>
              ))}
            </Select>
          </label>
          <Button onClick={actions.localSelectAll}>全选</Button>
          <Button onClick={actions.localInvert}>反选</Button>
          <Button onClick={actions.localClearSelection}>清除选择</Button>
          <Button variant="danger" disabled={!selectedCount} onClick={actions.localDelete}>
            删除
          </Button>
          <Button onClick={actions.localRestore}>恢复</Button>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Checkbox checked={state.showTrash} onCheckedChange={actions.setTrash} />
            回收站视图
          </label>
          <span className="ml-auto text-xs text-muted-foreground">
            已收录 <b className="text-foreground">{state.local.length}</b>
          </span>
          <span className="text-xs text-muted-foreground">
            已选 <b className="text-foreground">{selectedCount}</b>
          </span>
        </>
      )}

      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        列数
        <Slider
          value={state.columns}
          min={0}
          max={16}
          onChange={changeColumns}
        />
        <span className="w-6 text-center">{state.columns === 0 ? '自动' : state.columns}</span>
      </label>
    </div>
  );
}
