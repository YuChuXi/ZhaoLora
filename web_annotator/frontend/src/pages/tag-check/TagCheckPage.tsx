import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiPath, request } from '@/lib/api';
import { useApp } from '@/app/AppContext';
import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/input';
import { Checkbox, Slider } from '@/components/ui/controls';
import { Toolbar, ToolbarDivider } from '@/components/ui/toolbar';
import { useStatus } from '@/components/ui/status';
import { PageHint } from '@/components/ui/hint';
import { errorMessage, insertPositionOf, checkPositionOf, metricOf, parseSettings, positionText, toUiItem, type EmbOptions, type Engine, type ModeOption, type ParsedSettings, type SemanticItem, type SepOption, type TagCheckItem, type TagCheckResponse, type TagCheckResponseItem, type TagCheckSettings, type TagStatRow, type TagStatsResponse } from './model';
import { captureCardAnchor, restoreCardAnchor, useScrollLock, type CardAnchor } from '@/lib/scroll';
import { TagAutocomplete } from './TagAutocomplete';
import { TagCheckMasonry } from './TagCheckMasonry';
import { SingleView } from './SingleView';
import { SemanticDialog } from './SemanticDialog';

/**
 * 标签检查页：检查图片旁路 `.txt` 中标签的存在性与位置，支持瀑布流增删标签、
 * 单图模式、语义排序、批量标记与蒙板预览。
 *
 * Args:
 *   无。
 *
 * Returns:
 *   JSX.Element: 页面节点。
 *
 * Callers:
 *   - `frontend/src/main.tsx:PAGES`
 */
export function TagCheckPage() {
  const app = useApp();
  const initial = useMemo<ParsedSettings>(() => parseSettings(app.settings.tag_check), []);
  const [tag, setTag] = useState('');
  const [sep, setSep] = useState<SepOption>(initial.position);
  const [mode, setMode] = useState<ModeOption>(initial.mode);
  const [applyMask, setApplyMask] = useState(initial.applyMask);
  const [columns, setColumns] = useState(initial.cols);
  const [items, setItems] = useState<TagCheckItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [flash, setFlash] = useState<Record<string, boolean>>({});
  const [singleIndex, setSingleIndex] = useState(-1);
  const [stripOpen, setStripOpen] = useState(false);
  const [fit, setFit] = useState<'contain' | 'cover'>('contain');
  const [sortActive, setSortActive] = useState(false);
  const [semanticOpen, setSemanticOpen] = useState(false);
  const [tagStats, setTagStats] = useState<TagStatRow[]>([]);
  const [embOptions, setEmbOptions] = useState<EmbOptions>(initial.emb);

  const flashTimers = useRef<Record<string, number>>({});
  const columnAnchor = useRef<CardAnchor | null>(null);
  const settingsRef = useRef<TagCheckSettings>({
    position: initial.position,
    mode: initial.mode,
    apply_mask: initial.applyMask,
    cols: initial.cols,
    emb: initial.emb,
  });
  const itemsRef = useRef(items);
  const busyRef = useRef(busy);
  const singleIndexRef = useRef(singleIndex);
  const columnsRef = useRef(columns);
  const embRef = useRef(embOptions);
  const paramsRef = useRef({ tag, sep, mode });
  const statsSeq = useRef(0);

  itemsRef.current = items;
  busyRef.current = busy;
  singleIndexRef.current = singleIndex;
  columnsRef.current = columns;
  embRef.current = embOptions;
  paramsRef.current = { tag, sep, mode };

  const singleOpen = singleIndex >= 0;

  /**
   * 将页面设置合并写入全局 `tag_check` 键。
   *
   * Args:
   *   patch (Partial<TagCheckSettings>): 本次变更的设置项。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
   */
  const persist = useCallback(
    (patch: Partial<TagCheckSettings>) => {
      settingsRef.current = { ...settingsRef.current, ...patch };
      app.saveSettings({ tag_check: settingsRef.current });
    },
    [app],
  );

  /**
   * 按当前标签与位置检查目录内全部图片，可保留既有排序与指标。
   *
   * Args:
   *   preserveSort (boolean): 为 true 时仅按路径原位更新标签状态，保留语义排序与指标。
   *   tagOverride (string): 可选的本次检查标签，用于补全选中后立即提交。
   *
   * Returns:
   *   Promise<void>
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage` 检查按钮、目录变化、位置变化
   *   - `pages/tag-check/TagCheckPage.tsx:onBatchDone`
   */
  const runCheck = useCallback(
    async (preserveSort: boolean, tagOverride?: string) => {
      const value = (tagOverride ?? paramsRef.current.tag).trim();
      const position = checkPositionOf(paramsRef.current.sep, paramsRef.current.mode);
      if (!preserveSort) setSortActive(false);
      setLoading(true);
      try {
        const response = await request<TagCheckResponse>(apiPath('/tags/check'), {
          method: 'POST',
          body: JSON.stringify({ dirs: app.dirs, tag: value, position }),
        });
        if (preserveSort) {
          setItems((current) => {
            const byPath = new Map(response.items.map((item) => [item.path, item]));
            return current.map((item) => {
              const updated = byPath.get(item.path);
              return updated ? toUiItem(updated, item) : item;
            });
          });
        } else {
          setItems(response.items.map((item) => toUiItem(item)));
        }
        setSingleIndex(-1);
        setStripOpen(false);
      } catch (error) {
        app.notify(`检查失败: ${errorMessage(error)}`);
      } finally {
        setLoading(false);
      }
    },
    [app.dirs, app.notify],
  );
  const runCheckRef = useRef(runCheck);
  runCheckRef.current = runCheck;

  /**
   * 加载当前目录的标签统计，作为补全数据源；过期响应直接丢弃。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage` 目录变化 effect
   */
  const loadTagStats = useCallback(async () => {
    const requestId = ++statsSeq.current;
    try {
      const response = await request<TagStatsResponse>(apiPath('/tags/stats'), {
        method: 'POST',
        body: JSON.stringify({ dirs: app.dirs, min_count: 1, per_sample: true }),
      });
      if (requestId !== statsSeq.current) return;
      setTagStats(
        (response.tags ?? []).map((item) => ({
          tag: item.tag,
          count: item.count,
          zh: item.zh || '',
        })),
      );
    } catch (error) {
      if (requestId !== statsSeq.current) return;
      setTagStats([]);
      app.notify(`标签统计加载失败: ${errorMessage(error)}`);
    }
  }, [app.dirs, app.notify]);

  useEffect(() => {
    void loadTagStats();
    void runCheckRef.current(false);
  }, [loadTagStats]);

  useEffect(
    () => () => {
      for (const timer of Object.values(flashTimers.current)) window.clearTimeout(timer);
    },
    [],
  );

  /**
   * 为卡片触发写入成功的高亮反馈，800 毫秒后自动解除。
   *
   * Args:
   *   path (string): 卡片对应图片路径。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:onCardClick`
   */
  const flashCard = useCallback((path: string) => {
    window.clearTimeout(flashTimers.current[path]);
    setFlash((current) => ({ ...current, [path]: true }));
    flashTimers.current[path] = window.setTimeout(() => {
      delete flashTimers.current[path];
      setFlash((current) => {
        const next = { ...current };
        delete next[path];
        return next;
      });
    }, 800);
  }, []);

  /**
   * 调整列数前记录首个可见卡片的位置，重排后按该锚点恢复滚动偏移。
   *
   * Args:
   *   value (number): 目标列数，0 表示自动。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage` 列数滑块
   */
  const changeColumns = useCallback((value: number) => {
    columnAnchor.current = captureCardAnchor();
    setColumns(value);
  }, []);

  useEffect(() => {
    const anchor = columnAnchor.current;
    if (!anchor) return;
    const frame = window.requestAnimationFrame(() => {
      columnAnchor.current = null;
      restoreCardAnchor(anchor);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [columns]);

  useScrollLock(singleOpen);

  /**
   * 写入单张图片的标签状态，返回后端最新检查结果。
   *
   * Args:
   *   path (string): 图片相对路径。
   *   remove (boolean): 为 true 时删除标签，否则按工具栏位置添加。
   *
   * Returns:
   *   Promise<TagCheckResponseItem>: 写入后的检查结果。
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:onCardClick`
   *   - `pages/tag-check/TagCheckPage.tsx:editSingle`
   */
  const writeTag = useCallback(
    async (path: string, remove: boolean): Promise<TagCheckResponseItem> => {
      const value = paramsRef.current.tag.trim();
      if (remove) {
        return request<TagCheckResponseItem>(apiPath('/tags/delete-one'), {
          method: 'POST',
          body: JSON.stringify({ path, tag: value }),
        });
      }
      return request<TagCheckResponseItem>(apiPath('/tags/edit-one'), {
        method: 'POST',
        body: JSON.stringify({
          path,
          tag: value,
          position: insertPositionOf(paramsRef.current.sep, paramsRef.current.mode),
          check_position: checkPositionOf(paramsRef.current.sep, paramsRef.current.mode),
        }),
      });
    },
    [],
  );

  /**
   * 瀑布流卡片单击：标签存在则删除，不存在则添加；Shift 单击改为连选。
   *
   * Args:
   *   path (string): 卡片路径。
   *   shiftKey (boolean): 是否按住 Shift。
   *
   * Returns:
   *   Promise<void>
   *
   * Callers:
   *   - `pages/tag-check/TagCheckMasonry.tsx:TagCheckMasonry`
   */
  const onCardClick = useCallback(
    async (path: string) => {
      if (!paramsRef.current.tag.trim()) {
        app.notify('请先输入标签');
        return;
      }
      if (busyRef.current[path]) return;
      const item = itemsRef.current.find((entry) => entry.path === path);
      if (!item) return;
      setBusy((current) => ({ ...current, [path]: true }));
      try {
        const updated = await writeTag(path, item.exists);
        setItems((current) =>
          current.map((entry) => (entry.path === path ? toUiItem(updated, entry) : entry)),
        );
        flashCard(path);
      } catch (error) {
        app.notify(`操作失败: ${errorMessage(error)}`);
      } finally {
        setBusy((current) => {
          const next = { ...current };
          delete next[path];
          return next;
        });
      }
    },
    [app.notify, flashCard, writeTag],
  );

  /**
   * 单图模式内添加或删除标签，成功后自动跳到下一张。
   *
   * Args:
   *   remove (boolean): 为 true 时删除标签。
   *
   * Returns:
   *   Promise<void>
   *
   * Callers:
   *   - `pages/tag-check/SingleView.tsx:SingleView` 左键/右键
   */
  const editSingle = useCallback(
    async (remove: boolean) => {
      const index = singleIndexRef.current;
      const item = itemsRef.current[index];
      if (!item) return;
      if (!paramsRef.current.tag.trim()) {
        app.notify('请先输入标签');
        return;
      }
      if (busyRef.current[item.path]) return;
      setBusy((current) => ({ ...current, [item.path]: true }));
      try {
        const updated = await writeTag(item.path, remove);
        setItems((current) =>
          current.map((entry) => (entry.path === item.path ? toUiItem(updated, entry) : entry)),
        );
        const count = itemsRef.current.length;
        if (count) setSingleIndex((index + 1) % count);
      } catch (error) {
        app.notify(`操作失败: ${errorMessage(error)}`);
      } finally {
        setBusy((current) => {
          const next = { ...current };
          delete next[item.path];
          return next;
        });
      }
    },
    [app.notify, writeTag],
  );

  /**
   * 按路径进入单图模式。
   *
   * Args:
   *   path (string): 图片路径。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage` 右键/长按
   */
  const openSingle = useCallback((path: string) => {
    const index = itemsRef.current.findIndex((item) => item.path === path);
    if (index < 0) return;
    setSingleIndex(index);
    setStripOpen(false);
  }, []);

  /**
   * 退出单图模式并收起预览条。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
   */
  const closeSingle = useCallback(() => {
    setSingleIndex(-1);
    setStripOpen(false);
  }, []);

  /**
   * 在单图模式中前后切换图片。
   *
   * Args:
   *   delta (number): +1 下一张，-1 上一张。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
   */
  const navigate = useCallback((delta: number) => {
    const count = itemsRef.current.length;
    if (!count) return;
    setSingleIndex((current) => (current < 0 ? current : (current + delta + count) % count));
  }, []);

  /**
   * 将语义排序结果写回指标并降序重排，无指标项排在末尾。
   *
   * Args:
   *   engine (Engine): 产生结果的引擎。
   *   hits (SemanticItem[]): 后端排序结果。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog`
   */
  const applySort = useCallback((engine: Engine, hits: SemanticItem[]) => {
    const simMap = new Map<string, number>();
    const ratioMap = new Map<string, number>();
    const maskMap = new Map<string, string>();
    for (const hit of hits) {
      if (hit.similarity != null) simMap.set(hit.path, hit.similarity);
      if (hit.ratio != null) ratioMap.set(hit.path, hit.ratio);
      if (hit.mask_data) maskMap.set(hit.path, hit.mask_data);
    }
    setItems((current) => {
      const next = current.map((item) => ({
        ...item,
        sim: engine === 'similarity' ? simMap.get(item.path) ?? null : null,
        ratio: engine === 'concept' ? ratioMap.get(item.path) ?? null : null,
        mask: engine === 'concept' ? maskMap.get(item.path) ?? null : null,
      }));
      next.sort((left, right) => {
        const leftValue = metricOf(left, engine) ?? -Infinity;
        const rightValue = metricOf(right, engine) ?? -Infinity;
        if (leftValue === rightValue) return 0;
        return rightValue - leftValue;
      });
      return next;
    });
    setSortActive(true);
    setSingleIndex(-1);
    setStripOpen(false);
  }, []);

  /**
   * 切换语义检索引擎，清空既有排序指标并返回瀑布流。
   *
   * Args:
   *   engine (Engine): 目标引擎。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog`
   */
  const changeEngine = useCallback(
    (engine: Engine) => {
      if (embRef.current.engine === engine) return;
      const next = { ...embRef.current, engine };
      setEmbOptions(next);
      persist({ emb: next });
      setItems((current) =>
        current.map((item) => ({ ...item, sim: null, ratio: null, mask: null })),
      );
      setSortActive(false);
      setSingleIndex(-1);
      setStripOpen(false);
    },
    [persist],
  );

  /**
   * 更新并持久化批量标记参数。
   *
   * Args:
   *   patch (Partial<EmbOptions>): 本次变更的参数。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog`
   */
  const changeEmbOptions = useCallback(
    (patch: Partial<EmbOptions>) => {
      const next = { ...embRef.current, ...patch };
      setEmbOptions(next);
      persist({ emb: next });
    },
    [persist],
  );

  /**
   * 批量写入完成后刷新标签状态，保留排序与指标。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>
   *
   * Callers:
   *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog`
   */
  const onBatchDone = useCallback(async () => {
    await runCheckRef.current(true);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tagName = target?.tagName;
      if (tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT') return;
      if (singleOpen) {
        if (event.key === 'ArrowLeft' || event.key === 'a' || event.key === 'A') {
          event.preventDefault();
          navigate(-1);
        } else if (event.key === 'ArrowRight' || event.key === 'd' || event.key === 'D') {
          event.preventDefault();
          navigate(1);
        } else if (event.key === 'Escape') {
          event.preventDefault();
          closeSingle();
        }
        return;
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [closeSingle, navigate, singleOpen]);

  const okCount = items.reduce((total, item) => total + (item.position_ok ? 1 : 0), 0);
  const insertPosition = insertPositionOf(sep, mode);

  useStatus(
    singleOpen
      ? []
      : [
          { kind: 'total', value: items.length },
          { kind: 'part', name: '符合', value: okCount },
          { kind: 'part', name: '不符', value: items.length - okCount },
        ],
  );

  return (
    <div className={singleOpen ? 'flex min-h-0 flex-1 flex-col' : undefined}>
      <PageHint>
        {singleOpen
          ? '←/A →/D 上一张/下一张 · Esc 返回瀑布流'
          : '点击卡片增删标签 · 右键/长按进入单图'}
      </PageHint>
      <style>{`
        .masonry-card[data-flash='true'] {
          border-color: #5eb8ff !important;
          box-shadow: 0 0 0 2px #5eb8ff, 0 8px 24px rgba(94, 184, 255, 0.35) !important;
          transition: box-shadow 0.2s ease, border-color 0.2s ease;
        }
      `}</style>
      <Toolbar>
        <TagAutocomplete
          value={tag}
          tags={tagStats}
          placeholder="标签（如 1girl）"
          onChange={setTag}
          onSubmit={(value) => void runCheck(sortActive, value)}
        />
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          位置
          <Select
            value={sep}
            onChange={(event) => {
              const next = event.target.value as SepOption;
              setSep(next);
              paramsRef.current = { ...paramsRef.current, sep: next };
              persist({ position: next });
              void runCheck(sortActive);
            }}
          >
            <option value="any">任意位置</option>
            <option value="before">在 ||| 前</option>
            <option value="after">在 ||| 后</option>
          </Select>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          模式
          <Select
            value={mode}
            onChange={(event) => {
              const next = event.target.value as ModeOption;
              setMode(next);
              persist({ mode: next });
            }}
          >
            <option value="top">置顶</option>
            <option value="bottom">置底</option>
            <option value="random">任意</option>
          </Select>
        </label>

        {singleOpen ? (
          <>
            <ToolbarDivider />
            <Button title="返回瀑布流 (Esc)" onClick={closeSingle}>
              ✕ 瀑布流
            </Button>
            <Button title="上一张 (←/A)" onClick={() => navigate(-1)}>
              ◀
            </Button>
            <Button title="下一张 (→/D)" onClick={() => navigate(1)}>
              ▶
            </Button>
            <Button
              title="切换 contain/cover"
              onClick={() => setFit((current) => (current === 'contain' ? 'cover' : 'contain'))}
            >
              {fit === 'cover' ? '⊟' : '⊞'}
            </Button>
          </>
        ) : (
          <>
            <Button onClick={() => setSemanticOpen(true)} title="相似度搜索与批量标记">
              相似度
            </Button>
            <ToolbarDivider />
            <Button variant="primary" disabled={loading} onClick={() => void runCheck(sortActive)}>
              检查
            </Button>
            <label
              className="flex items-center gap-1.5 text-xs text-muted-foreground"
              title="将每张图的蒙板应用到图片(去背), 透明区显示棋盘格"
            >
              <Checkbox
                checked={applyMask}
                onCheckedChange={(checked) => {
                  setApplyMask(checked);
                  persist({ apply_mask: checked });
                  setSingleIndex(-1);
                  setStripOpen(false);
                }}
              />
              蒙板
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              列
              <Slider
                value={columns}
                min={0}
                max={16}
                onChange={changeColumns}
                onCommit={() => persist({ cols: columnsRef.current })}
              />
              <span className="w-6 text-center">{columns === 0 ? '自动' : columns}</span>
            </label>
            <Button
              onClick={() => {
                if (items.length) {
                  setSingleIndex(0);
                  setStripOpen(false);
                }
              }}
            >
              单图
            </Button>
            <ToolbarDivider />
          </>
        )}
      </Toolbar>

      <div className={singleOpen ? 'relative min-h-0 flex-1 overflow-hidden' : undefined}>
        <TagCheckMasonry
          items={items}
          columns={columns}
          busy={busy}
          flash={flash}
          applyMask={applyMask}
          onCardClick={(path) => void onCardClick(path)}
          onCardContext={openSingle}
        />
        <div className="px-3 py-2 text-xs text-muted-foreground">
          输入标签并检查 · 绿色=位置符合 · 红色=位置不符 · 点击卡片添加/删除标签 · 右键卡片进入单图 ·
          无标签时显示全部图片 · 支持相似度排序、概念分割与批量标记
        </div>

        {singleOpen ? (
          <SingleView
            items={items}
            index={singleIndex}
            fit={fit}
            stripOpen={stripOpen}
            onSelectIndex={setSingleIndex}
            onToggleStrip={() => setStripOpen((current) => !current)}
            onAdd={() => void editSingle(false)}
            onDelete={() => void editSingle(true)}
          />
        ) : null}
      </div>

      <SemanticDialog
        open={semanticOpen}
        onClose={() => setSemanticOpen(false)}
        dirs={app.dirs}
        engine={embOptions.engine}
        options={embOptions}
        onEngineChange={changeEngine}
        onOptionsChange={changeEmbOptions}
        positionText={positionText(sep, mode)}
        insertPosition={insertPosition}
        items={items}
        sortActive={sortActive}
        onSorted={applySort}
        onBatchDone={onBatchDone}
        notify={app.notify}
      />
    </div>
  );
}
