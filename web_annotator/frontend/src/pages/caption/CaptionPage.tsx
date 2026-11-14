import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '@/lib/api';
import type {
  BatchSnapshot,
  CaptionDirsData,
  CaptionImage,
  CaptionSettings,
} from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Dialog } from '@/components/ui/dialog';
import { useApp } from '@/app/AppContext';
import { Masonry, type ViewItem } from '@/filter/Masonry';
import { CaptionSettingsDialog } from './CaptionSettings';

const MODE_LABELS: Record<string, string> = { coarse: '粗略', detailed: '详细' };

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function danbooruRe(pattern: string): RegExp {
  const body = pattern.toLowerCase().replace(/_/g, ' ').split('*').map(escapeRegExp).join('.*');
  return new RegExp(`^${body}$`);
}

function compileRe(source: string): RegExp {
  const inline = /^\(\?([imsx]+)\)/.exec(source);
  if (!inline) return new RegExp(source);
  return new RegExp(source.slice(inline[0].length), inline[1].replace(/[^ims]/g, ''));
}

function compileMatcher(mode: string, query: string): ((text: string) => boolean) | null {
  if (!query) return null;
  if (mode === 'exact') return (text) => text.indexOf(query) !== -1;
  if (mode === 'danbooru') {
    const re = danbooruRe(query);
    return (text) => re.test(text.toLowerCase().replace(/_/g, ' ').trim());
  }
  try {
    const re = compileRe(query);
    return (text) => re.test(text);
  } catch {
    return null;
  }
}

function defaultSettings(data: CaptionDirsData | null, saved: unknown): CaptionSettings {
  const source = (saved && typeof saved === 'object' ? saved : {}) as Partial<CaptionSettings>;
  const inject = source.inject ?? {};
  const fallback = String(data?.default_wd14_threshold ?? 0.35);
  return {
    modes: source.modes?.length ? source.modes : data?.modes ?? ['coarse', 'detailed'],
    inject: {
      general: !!inject.general,
      general_threshold: inject.general_threshold || fallback,
      character: !!inject.character,
      character_threshold: inject.character_threshold || fallback,
      nsfw: !!inject.nsfw,
      nsfw_threshold: inject.nsfw_threshold || fallback,
    },
    force: !!source.force,
    rules: source.rules ?? [],
    groups: source.groups ?? [],
  };
}

export function CaptionPage() {
  const app = useApp();
  const [data, setData] = useState<CaptionDirsData | null>(null);
  const [items, setItems] = useState<CaptionImage[]>([]);
  const [settings, setSettings] = useState<CaptionSettings>(defaultSettings(null, app.settings.caption));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [filterMode, setFilterMode] = useState('exact');
  const [filterText, setFilterText] = useState('');
  const [filterQuery, setFilterQuery] = useState('');
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [view, setView] = useState<CaptionImage | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<BatchSnapshot | null>(null);
  const filterTimer = useRef<number>();
  const booted = useRef(false);
  const settingsRef = useRef(app.settings.caption);
  settingsRef.current = app.settings.caption;

  const reload = useCallback(
    async (dirs: string[]) => {
      const result = await api.captionList(dirs);
      setItems(result.items ?? []);
      setSelected({});
      setCursor(null);
    },
    [],
  );

  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    let cancelled = false;
    (async () => {
      try {
        const result = await api.captionDirs();
        if (cancelled) return;
        setData(result);
        setSettings(defaultSettings(result, settingsRef.current));
        const active = await api.findActiveJob('caption');
        if (!cancelled && active?.job_id) setJobId(active.job_id);
      } catch (error) {
        if (!cancelled) app.notify(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [app]);

  useEffect(() => {
    if (!data) return;
    void reload(app.dirs).catch((error) =>
      app.notify(error instanceof Error ? error.message : String(error)),
    );
  }, [app.dirs, data, reload, app]);

  useEffect(() => {
    if (!jobId) return;
    return api.watchJob(jobId, (snap) => {
      setSnapshot(snap);
      if (snap.status === 'done' || snap.status === 'error' || snap.status === 'cancelled') {
        setJobId(null);
        void reload(app.dirs);
      }
    });
  }, [jobId, reload, app.dirs]);

  const filtered = useMemo(() => {
    const matcher = compileMatcher(filterMode, filterQuery.trim());
    if (!matcher) return items;
    return items.filter((item) => (item.captions || []).some(matcher));
  }, [items, filterMode, filterQuery]);

  const viewItems = useMemo<ViewItem[]>(
    () =>
      filtered.map((item) => ({
        key: item.path,
        ratio: item.width > 0 && item.height > 0 ? item.height / item.width : 1,
        src: item.thumbnail_hash ? api.thumbHashUrl(item.thumbnail_hash) : api.localImageUrl(item.path),
        label: item.stem,
        kind: 'image' as const,
      })),
    [filtered],
  );

  const selectedCount = useMemo(() => Object.values(selected).filter(Boolean).length, [selected]);
  const described = useMemo(
    () => filtered.filter((item) => item.captions?.length).length,
    [filtered],
  );

  const toggle = useCallback((path: string, shiftKey: boolean) => {
    setSelected((current) => {
      if (shiftKey && cursor) {
        const keys = filtered.map((item) => item.path);
        const from = keys.indexOf(cursor);
        const to = keys.indexOf(path);
        if (from >= 0 && to >= 0) {
          const next = { ...current };
          for (const key of keys.slice(Math.min(from, to), Math.max(from, to) + 1)) next[key] = true;
          return next;
        }
      }
      return { ...current, [path]: !current[path] };
    });
    setCursor(path);
  }, [cursor, filtered]);

  const deleteCaptions = useCallback(
    async (paths: string[], confirmFirst: boolean) => {
      if (!paths.length) {
        app.notify('未选择图片');
        return;
      }
      if (confirmFirst && !window.confirm(`删除 ${paths.length} 张图片的描述？`)) return;
      const result = await api.captionDelete(paths);
      app.notify(result.deleted ? `已删除 ${result.deleted} 张的描述` : '所选图片均无描述');
      setView(null);
      await reload(app.dirs);
    },
    [app, reload],
  );

  const saveSettings = useCallback(
    (next: CaptionSettings) => {
      setSettings(next);
      app.saveSettings({ caption: next }, true);
    },
    [app],
  );

  const startJob = useCallback(async () => {
    if (!settings.modes.length) {
      app.notify('请至少选择一种描述模式');
      return;
    }
    const paths = selectedCount ? Object.keys(selected).filter((key) => selected[key]) : filtered.map((item) => item.path);
    if (!paths.length) {
      app.notify('没有可处理的图片');
      return;
    }
    const threshold = (enabled: boolean | undefined, value: string | undefined) => {
      if (!enabled) return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    };
    try {
      const result = await api.captionStart({
        dirs: app.dirs,
        paths,
        modes: settings.modes,
        general_threshold: threshold(settings.inject.general, settings.inject.general_threshold),
        character_threshold: threshold(settings.inject.character, settings.inject.character_threshold),
        nsfw_threshold: threshold(settings.inject.nsfw, settings.inject.nsfw_threshold),
        rules: settings.rules.filter((rule) => rule.text.trim()),
        groups: settings.groups,
        force: settings.force,
      });
      setSettingsOpen(false);
      setSnapshot(null);
      setJobId(result.job_id);
    } catch (error) {
      app.notify(error instanceof Error ? error.message : String(error));
    }
  }, [app, filtered, selected, selectedCount, settings]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement)?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (event.key === 'a' && (event.ctrlKey || event.metaKey) && !event.shiftKey) {
        event.preventDefault();
        const next: Record<string, boolean> = {};
        for (const item of filtered) next[item.path] = true;
        setSelected(next);
      } else if (event.key === 'i' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        const next: Record<string, boolean> = {};
        for (const item of filtered) if (!selected[item.path]) next[item.path] = true;
        setSelected(next);
      } else if (event.key === 'Delete') {
        event.preventDefault();
        void deleteCaptions(Object.keys(selected).filter((key) => selected[key]), true);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [deleteCaptions, filtered, selected]);

  return (
    <div>
      <div className="sticky top-[41px] z-30 flex flex-wrap items-center gap-2 border-b border-border bg-background/95 px-2 py-2 backdrop-blur">
        <Button variant="primary" onClick={() => void startJob()} disabled={!!jobId}>
          生成描述
        </Button>
        <Button onClick={() => setSettingsOpen(true)}>设置</Button>
        {jobId ? (
          <Button
            variant="danger"
            onClick={() => {
              void api.cancelJob(jobId);
            }}
          >
            取消
          </Button>
        ) : null}
        <span className="mx-1 h-5 w-px bg-border" />
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          描述
          <Select value={filterMode} onChange={(event) => setFilterMode(event.target.value)}>
            <option value="exact">严格子串</option>
            <option value="danbooru">Danbooru 通配(*)</option>
            <option value="regex">正则</option>
          </Select>
        </label>
        <Select
          value=""
          onChange={(event) => {
            const preset = data?.filter_presets[Number(event.target.value)];
            if (!preset) return;
            setFilterMode(preset.mode);
            setFilterText(preset.pattern);
            setFilterQuery(preset.pattern);
          }}
          className="w-32"
        >
          <option value="">预置</option>
          {data?.filter_presets.map((preset, index) => (
            <option key={preset.label} value={index}>
              {preset.label}
            </option>
          ))}
        </Select>
        <Input
          className="w-[min(360px,40vw)]"
          placeholder="按描述筛选; 留空显示全部"
          value={filterText}
          onChange={(event) => {
            const value = event.target.value;
            setFilterText(value);
            window.clearTimeout(filterTimer.current);
            filterTimer.current = window.setTimeout(() => setFilterQuery(value), 120);
          }}
        />
        <span className="mx-1 h-5 w-px bg-border" />
        <Button
          onClick={() => {
            const next: Record<string, boolean> = {};
            for (const item of filtered) next[item.path] = true;
            setSelected(next);
          }}
        >
          全选
        </Button>
        <Button
          onClick={() => {
            const next: Record<string, boolean> = {};
            for (const item of filtered) if (!selected[item.path]) next[item.path] = true;
            setSelected(next);
          }}
        >
          反选
        </Button>
        <Button disabled={!selectedCount} onClick={() => setSelected({})}>
          清除选择
        </Button>
        <Button
          variant="danger"
          disabled={!selectedCount}
          onClick={() => void deleteCaptions(Object.keys(selected).filter((key) => selected[key]), true)}
        >
          删除描述
        </Button>
        <span className="ml-auto text-xs text-muted-foreground">
          共 <b className="text-foreground">{filtered.length}</b> · 已描述{' '}
          <b className="text-foreground">{described}</b> · 未描述{' '}
          <b className="text-foreground">{filtered.length - described}</b>
        </span>
        {selectedCount ? (
          <span className="text-xs text-muted-foreground">
            已选 <b className="text-foreground">{selectedCount}</b>
          </span>
        ) : null}
        <span className="text-xs text-muted-foreground">
          {data?.ready ? data.info || '模型就绪' : `未就绪: ${data?.info || ''}`}
        </span>
      </div>

      {snapshot ? (
        <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-xs text-muted-foreground">
          <div className="h-1 flex-1 overflow-hidden rounded bg-border">
            <div className="h-full bg-primary transition-all" style={{ width: `${Math.min(100, snapshot.pct ?? 0)}%` }} />
          </div>
          <span>{snapshot.message || `${Math.round(snapshot.pct ?? 0)}%`}</span>
        </div>
      ) : null}

      <Masonry
        items={viewItems}
        fixedColumns={0}
        selected={selected}
        status={{}}
        progress={{}}
        hasMore={false}
        hasPrev={false}
        loading={!data}
        onCardClick={toggle}
        onCardContext={(key) => {
          const item = items.find((entry) => entry.path === key);
          if (item) setView(item);
        }}
        onLoadMore={() => {}}
      />

      <CaptionSettingsDialog
        open={settingsOpen}
        data={data}
        value={settings}
        onClose={() => setSettingsOpen(false)}
        onSave={saveSettings}
      />

      <Dialog
        open={!!view}
        onOpenChange={(open) => !open && setView(null)}
        title={view?.path || '描述'}
        footer={
          <>
            {view?.captions?.length ? (
              <Button variant="danger" onClick={() => void deleteCaptions([view.path], false)}>
                删除描述
              </Button>
            ) : null}
            <Button onClick={() => setView(null)}>关闭</Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 text-xs text-muted-foreground">
          {view?.captions?.length ? (
            view.captions.map((caption, index) => (
              <div key={index} className="whitespace-pre-wrap rounded border border-border bg-[#0a1a30] px-2.5 py-2 text-[13px] leading-relaxed text-foreground">
                <span className="mb-1 block font-semibold text-muted-foreground">
                  {MODE_LABELS[settings.modes[index]] || `#${index + 1}`}
                </span>
                {caption}
              </div>
            ))
          ) : (
            <div>未描述</div>
          )}
        </div>
      </Dialog>
    </div>
  );
}
