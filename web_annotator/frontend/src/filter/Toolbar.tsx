import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
} from 'react';
import { api, SOURCES, type SourceLayout } from '@/lib/api';
import type { Mode, SortKey } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Checkbox, Slider } from '@/components/ui/controls';
import { TagSuggestInput } from '@/components/ui/tag-suggest';
import { Toolbar as ToolbarFrame } from '@/components/ui/toolbar';
import { captureCardAnchor, restoreCardAnchor, type CardAnchor } from '@/lib/scroll';
import type { FilterActions } from './useFilterPage';
import type { PageState } from './state';
import { conditionBadge, parseCondition } from './condition';
import { ConditionDialog } from './Dialogs';

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
  { value: 'search', label: '搜索' },
];

interface SectionProps {
  state: PageState;
  actions: FilterActions;
  selectedCount: number;
}

/**
 * 本地来源的工具栏操作集: 排序、选择与回收站.
 *
 * Args:
 *   state (PageState): 页面状态.
 *   actions (FilterActions): 页面动作.
 *   selectedCount (number): 选中数量.
 *
 * Returns:
 *   JSX.Element: 操作节点.
 *
 * Callers:
 *   - `filter/Toolbar.tsx:Toolbar`（经 `SECTIONS` 映射）
 */
function LocalSection({ state, actions, selectedCount }: SectionProps) {
  const sorts = useMemo(
    () => [
      ...SORTS,
      ...state.scoreItems.map((item) => ({
        value: `score:${item.key}` as SortKey,
        label: item.label,
      })),
    ],
    [state.scoreItems],
  );

  return (
    <>
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        排序
        <Select
          value={state.sort}
          onChange={(event) => {
            const next = event.target.value as SortKey;
            if (next === 'search') {
              actions.openSearch();
              return;
            }
            actions.setSort(next);
          }}
        >
          {sorts.map((sort) => (
            <option key={sort.value} value={sort.value}>
              {sort.label}
            </option>
          ))}
        </Select>
      </label>
      <Button onClick={actions.localSelectAll}>全选</Button>
      <Button onClick={actions.localInvert}>反选</Button>
      <Button onClick={actions.localClearSelection}>清除选择</Button>
      {state.search ? (
        <Button variant="ghost" onClick={actions.clearSearch}>
          清除搜索
        </Button>
      ) : null}
      <Button variant="danger" disabled={!selectedCount} onClick={actions.localDelete}>
        删除
      </Button>
      <Button onClick={actions.localRestore}>恢复</Button>
      <Button onClick={actions.openMeta}>元数据</Button>
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Checkbox checked={state.showTrash} onCheckedChange={actions.setTrash} />
        回收站视图
      </label>
    </>
  );
}

/**
 * 在线来源的工具栏检索集: 来源声明的字段、条件入口与翻页.
 *
 * 字段全部由 `SourceDef.fields` 驱动; 额外检索条件不占工具栏位置, 只经
 * "搜索条件"对话框编辑.
 *
 * Args:
 *   state (PageState): 页面状态.
 *   actions (FilterActions): 页面动作.
 *   selectedCount (number): 选中数量（检索集不使用，保持签名统一）.
 *
 * Returns:
 *   JSX.Element: 操作节点与条件对话框.
 *
 * Callers:
 *   - `filter/Toolbar.tsx:Toolbar`（经 `SECTIONS` 映射）
 */
function SearchSection({ state, actions }: SectionProps) {
  const def = SOURCES[state.mode];
  const fields = state.remote.fields;
  const condition = state.conditions[state.mode];
  const [conditionOpen, setConditionOpen] = useState(false);
  const [pageText, setPageText] = useState('');
  const pageRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (document.activeElement === pageRef.current) return;
    setPageText(String(state.remote.visiblePage));
  }, [state.remote.visiblePage]);
  const extra = fields.extra ?? condition?.default ?? '';
  const badge = useMemo(
    () => (condition ? conditionBadge(condition, parseCondition(condition, extra)) : null),
    [condition, extra],
  );
  const fetchTags = useCallback(
    (query: string, signal: AbortSignal) =>
      api.tagSuggest(state.mode, query, signal).then((result) => result.tags),
    [state.mode],
  );

  return (
    <>
      {def.fields.map((field) => {
        if (field.type === 'text') {
          if (!field.id) return null;
          if (field.id === 'tags') {
            return (
              <TagSuggestInput
                key={field.id}
                value={fields.tags ?? ''}
                placeholder={field.placeholder}
                className="flex-1"
                fetchTags={fetchTags}
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

      {condition ? (
        <>
          <Button
            className={badge && badge.count > 0 ? 'relative border-[#4a8] text-[#9cf]' : 'relative'}
            title={badge && badge.count > 0 ? badge.summary : '评级、分数、分辨率等'}
            onClick={() => setConditionOpen(true)}
          >
            搜索条件
            {badge && badge.count > 0 ? (
              <span className="ml-1 rounded-full bg-[#4a8] px-1 text-xs font-bold text-[#061018]">
                {badge.count}
              </span>
            ) : null}
          </Button>
          <ConditionDialog
            open={conditionOpen}
            condition={condition}
            extra={extra}
            onClose={() => setConditionOpen(false)}
            onApply={(next) => actions.setField('extra', next)}
          />
        </>
      ) : null}

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
          <Input
            ref={pageRef}
            type="number"
            min={1}
            value={pageText}
            className="h-full w-16 rounded-none border-0 border-x border-border bg-transparent text-center"
            title="页码；滚动时随当前页更新，回车跳页"
            onChange={(event) => setPageText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return;
              const page = Number((event.target as HTMLInputElement).value);
              if (Number.isInteger(page) && page > 0) actions.jumpPage(page);
              else setPageText(String(state.remote.visiblePage));
            }}
          />
          <button
            type="button"
            className="w-6 text-sm text-muted-foreground disabled:opacity-30"
            disabled={!state.remote.hasMore || state.remote.loading}
            onClick={() => actions.loadMore(false)}
          >
            ›
          </button>
        </div>
      ) : (
        <span className="flex h-7 items-center border-x border-border px-2 text-xs text-muted-foreground">
          第 {state.remote.visiblePage} 页
        </span>
      )}
    </>
  );
}

/** 来源布局到工具栏分区的映射; 新增布局只在此登记. */
const SECTIONS: Record<SourceLayout, ComponentType<SectionProps>> = {
  local: LocalSection,
  search: SearchSection,
};

export function Toolbar({
  state,
  actions,
}: {
  state: PageState;
  actions: FilterActions;
}) {
  const anchor = useRef<CardAnchor | null>(null);
  const selectedCount = useMemo(
    () => Object.values(state.selected).filter(Boolean).length,
    [state.selected],
  );
  const Section = SECTIONS[SOURCES[state.mode].layout];

  const changeColumns = (value: number) => {
    anchor.current = captureCardAnchor();
    actions.setColumns(value);
  };

  useLayoutEffect(() => {
    restoreCardAnchor(anchor.current);
    anchor.current = null;
  }, [state.columns]);

  return (
    <ToolbarFrame>
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

      <Section state={state} actions={actions} selectedCount={selectedCount} />

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
    </ToolbarFrame>
  );
}
