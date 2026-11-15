import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiPath, request } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Dialog } from '@/components/ui/dialog';
import { Toolbar, ToolbarDivider, ToolbarGroup } from '@/components/ui/toolbar';
import { useJob, useStatus } from '@/components/ui/status';
import { PageHint } from '@/components/ui/hint';
import { useApp } from '@/app/AppContext';
import { useScrollLock } from '@/lib/scroll';
import { Masonry, type ViewItem } from '@/filter/Masonry';

const AES_COLOURS = [
  '#c0392b', '#e74c3c', '#e67e22', '#f39c12', '#d4ac0d',
  '#27ae60', '#1e8449', '#148f77', '#0e6655', '#0a4f42',
];

interface ScoreItem {
  key: string;
  label: string;
  expr: string;
  builtin: boolean;
}

interface ScoringDirsResponse {
  aesthetic_ready: boolean;
  aesthetic_count: number;
  items: ScoreItem[];
  item_counts: Record<string, number>;
  wd14_ready: boolean;
  wd14_message: string;
  wd14_count: number;
  wd14_total: number;
}

interface ScoreResult {
  path: string;
  score: number;
  pony?: number;
  pony_label?: string;
  image_url: string;
  thumb_url: string;
}

interface ScoringResultsResponse {
  kind: string;
  total: number;
  results: ScoreResult[];
}

interface JobEvent {
  status: string;
  kind?: string;
  message?: string;
  done?: number;
  total?: number;
  failed?: number;
  pct?: number | null;
  phase?: string;
  current?: string;
  cancel_requested?: boolean;
  result?: unknown;
}

interface ScoreBins {
  lo: number;
  hi: number;
  step: number;
}

interface HistogramBin {
  value: string;
  colour: string;
  label: string;
  count: number;
}

interface ScoreStats {
  mean: number | null;
  median: number | null;
  std: number | null;
  min: number | null;
  max: number | null;
  count: number;
}

/**
 * 从设置值中读取非空字符串。
 *
 * Args:
 *   value (unknown): 设置中的原始值。
 *   fallback (string): 无有效字符串时的缺省值。
 *
 * Returns:
 *   string: 有效字符串或缺省值。
 *
 * Callers:
 *   - `ScoringPage`：初始化排序、评分项与筛选状态。
 */
function savedString(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback;
}

/**
 * 将未知异常转换为可展示的文本。
 *
 * Args:
 *   error (unknown): 捕获到的异常。
 *
 * Returns:
 *   string: 异常消息；非 Error 时取字符串形式。
 *
 * Callers:
 *   - `ScoringPage`：请求失败提示。
 */
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 取路径中的文件名。
 *
 * Args:
 *   path (string): 图片相对路径。
 *
 * Returns:
 *   string: 最后一段文件名。
 *
 * Callers:
 *   - `ScoringPage`：卡片信息行展示。
 */
function fileName(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1];
}

/**
 * 计算评分项直方图的十档边界。
 *
 * Args:
 *   results (ScoreResult[]): 当前评分项的全部结果。
 *
 * Returns:
 *   ScoreBins: 下界、上界与等宽步长。
 *
 * Callers:
 *   - `ScoringPage`：自定义评分项的分档与筛选。
 */
function scoreBins(results: ScoreResult[]): ScoreBins {
  let lo = Infinity;
  let hi = -Infinity;
  for (const result of results) {
    if (result.score < lo) lo = result.score;
    if (result.score > hi) hi = result.score;
  }
  if (!Number.isFinite(lo)) {
    lo = 0;
    hi = 1;
  }
  if (hi - lo < 1e-9) hi = lo + 1e-6;
  return { lo, hi, step: (hi - lo) / 10 };
}

/**
 * 将分值映射到十档中的一档。
 *
 * Args:
 *   value (number): 分值。
 *   bins (ScoreBins): 分档边界。
 *
 * Returns:
 *   number: 0-9 的档位序号。
 *
 * Callers:
 *   - `ScoringPage`：直方图统计与档位筛选。
 */
function scoreBin(value: number, bins: ScoreBins): number {
  return Math.min(9, Math.max(0, Math.floor((value - bins.lo) / bins.step)));
}

/**
 * 计算均值、中位数、标准差、极值与数量。
 *
 * Args:
 *   values (number[]): 待统计的分值。
 *
 * Returns:
 *   ScoreStats: 统计结果；空数组时数值项为 null。
 *
 * Callers:
 *   - `ScoringPage`：统计栏渲染。
 */
function computeStats(values: number[]): ScoreStats {
  const count = values.length;
  if (!count) {
    return { mean: null, median: null, std: null, min: null, max: null, count };
  }
  const sum = values.reduce((acc, value) => acc + value, 0);
  const mean = sum / count;
  const sorted = values.slice().sort((a, b) => a - b);
  const median = count % 2
    ? sorted[(count - 1) / 2]
    : (sorted[count / 2 - 1] + sorted[count / 2]) / 2;
  const variance = values.reduce((acc, value) => acc + (value - mean) ** 2, 0) / count;
  return {
    mean,
    median,
    std: Math.sqrt(variance),
    min: sorted[0],
    max: sorted[count - 1],
    count,
  };
}

/**
 * 判断评分项 key 是否仍然存在。
 *
 * Args:
 *   kind (string): 评分项 key 或 `aesthetic`。
 *   items (ScoreItem[]): 当前评分项列表。
 *
 * Returns:
 *   boolean: 是否为合法评分项。
 *
 * Callers:
 *   - `ScoringPage`：加载目录信息后校正标签页。
 */
function isKnownKind(kind: string, items: ScoreItem[]): boolean {
  return kind === 'aesthetic' || items.some((item) => item.key === kind);
}

/**
 * 取评分项的显示名称。
 *
 * Args:
 *   kind (string): 评分项 key 或 `aesthetic`。
 *   items (ScoreItem[]): 当前评分项列表。
 *
 * Returns:
 *   string: 显示名称；未知 key 原样返回。
 *
 * Callers:
 *   - `ScoringPage`：灯箱标签文字。
 */
function kindLabel(kind: string, items: ScoreItem[]): string {
  if (kind === 'aesthetic') return '美学';
  return items.find((item) => item.key === kind)?.label ?? kind;
}

/**
 * 构造十档直方图的分档信息与档位下拉选项。
 *
 * Args:
 *   kind (string): 当前评分项 key。
 *   results (ScoreResult[]): 已按当前排序排列的全部结果。
 *   bins (ScoreBins | null): 自定义评分项的分档边界。
 *
 * Returns:
 *   HistogramBin[]: 每档的筛选取值、颜色、标签与数量。
 *
 * Callers:
 *   - `ScoringPage`：直方图与档位筛选下拉。
 */
function buildHistogram(
  kind: string,
  results: ScoreResult[],
  bins: ScoreBins | null,
): HistogramBin[] {
  const out: HistogramBin[] = [];
  if (kind === 'aesthetic') {
    const counts = new Array(10).fill(0) as number[];
    for (const result of results) counts[result.pony ?? 0] += 1;
    for (let index = 0; index < 10; index += 1) {
      out.push({
        value: String(index),
        colour: AES_COLOURS[index],
        label: `score_${index}`,
        count: counts[index],
      });
    }
    return out;
  }
  if (!bins) return out;
  const counts = new Array(10).fill(0) as number[];
  for (const result of results) counts[scoreBin(result.score, bins)] += 1;
  for (let index = 0; index < 10; index += 1) {
    const lo = bins.lo + bins.step * index;
    const hi = lo + bins.step;
    out.push({
      value: String(index),
      colour: `hsl(${200 - Math.round((index * 200) / 9)},72%,52%)`,
      label: `${lo.toFixed(2)}–${hi.toFixed(2)}`,
      count: counts[index],
    });
  }
  return out;
}

/**
 * 统一评分页：美学与 WD14 评分项结果浏览、评分任务、WD14 缓存与评分项管理。
 *
 * Args:
 *   props (object): 无外部属性，数据集目录与设置由 `useApp` 提供。
 *
 * Returns:
 *   JSX.Element: 评分页界面。
 *
 * Callers:
 *   - `main.tsx`：评分路由挂载。
 */
export function ScoringPage() {
  const { dirs, notify, saveSettings, setDirsDisabled, settings } = useApp();
  const saved = (settings.scoring ?? {}) as Record<string, unknown>;
  const dirKey = dirs.join(',');

  const [items, setItems] = useState<ScoreItem[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [wd14, setWd14] = useState({
    ready: false,
    count: 0,
    total: 0,
    message: '',
  });
  const [kind, setKind] = useState<string>(savedString(saved.kind, 'aesthetic'));
  const [sortKey, setSortKey] = useState<'score' | 'path'>(
    saved.sort === 'path' ? 'path' : 'score',
  );
  const [sortDesc, setSortDesc] = useState(saved.desc !== false);
  const [scoreFilter, setScoreFilter] = useState(savedString(saved.filter, 'all'));
  const [query, setQuery] = useState('');
  const [resultsByKind, setResultsByKind] = useState<Record<string, ScoreResult[]>>({});
  const [loadedKinds, setLoadedKinds] = useState<Record<string, boolean>>({});
  const [dirsReady, setDirsReady] = useState(false);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [lightbox, setLightbox] = useState<string | null>(null);
  const [itemsOpen, setItemsOpen] = useState(false);
  const [itemLabel, setItemLabel] = useState('');
  const [itemExpr, setItemExpr] = useState('');
  const [snapshot, setSnapshot] = useState<JobEvent | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const jobMode = useRef<'scoring' | 'cache'>('scoring');
  const epoch = useRef(0);
  const resumed = useRef(false);

  const results = resultsByKind[kind] ?? [];
  const running = snapshot?.status === 'running' || snapshot?.status === 'queued';

  /**
   * 将评分页状态写入全局设置键 `scoring`。
   *
   * Args:
   *   patch (object): 需要同步覆盖的字段。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ScoringPage`：切换标签页、排序与档位筛选。
   */
  const persistScoring = useCallback(
    (patch: Partial<{ kind: string; sort: 'score' | 'path'; desc: boolean; filter: string }>) => {
      saveSettings({
        scoring: {
          kind,
          sort: sortKey,
          desc: sortDesc,
          filter: scoreFilter,
          ...patch,
        },
      });
    },
    [kind, saveSettings, scoreFilter, sortDesc, sortKey],
  );

  /**
   * 拉取评分项、各评分项数量与 WD14 缓存状态。
   *
   * Returns:
   *   Promise<void>: 请求完成后更新页面状态。
   *
   * Callers:
   *   - `ScoringPage`：目录变化、任务完成、评分项增删后刷新。
   */
  const loadDirs = useCallback(async () => {
    const data = await request<ScoringDirsResponse>(apiPath('/scoring/dirs'));
    setItems(data.items ?? []);
    setWd14({
      ready: !!data.wd14_ready,
      count: data.wd14_count ?? 0,
      total: data.wd14_total ?? 0,
      message: data.wd14_message ?? '',
    });
    setCounts((current) => {
      const next: Record<string, number> = {
        ...current,
        aesthetic: data.aesthetic_count ?? 0,
      };
      for (const item of data.items ?? []) {
        next[item.key] = data.item_counts?.[item.key] ?? 0;
      }
      return next;
    });
    setKind((current) => (isKnownKind(current, data.items ?? []) ? current : 'aesthetic'));
    setDirsReady(true);
  }, []);

  /**
   * 拉取指定评分项的结果列表，并忽略目录切换后返回的过期响应。
   *
   * Args:
   *   target (string): 评分项 key 或 `aesthetic`。
   *
   * Returns:
   *   Promise<void>: 结果写入页面状态。
   *
   * Callers:
   *   - `ScoringPage`：标签页切换与目录变化后的数据加载。
   */
  const loadResults = useCallback(async (target: string) => {
    const stamp = epoch.current;
    try {
      const data = await request<ScoringResultsResponse>(
        apiPath(`/scoring/results?kind=${encodeURIComponent(target)}`),
      );
      if (stamp !== epoch.current) return;
      setResultsByKind((current) => ({ ...current, [target]: data.results ?? [] }));
      setCounts((current) => ({ ...current, [target]: data.total ?? 0 }));
      setLoadedKinds((current) => ({ ...current, [target]: true }));
    } catch (error) {
      if (stamp !== epoch.current) return;
      setResultsByKind((current) => ({ ...current, [target]: [] }));
      setLoadedKinds((current) => ({ ...current, [target]: true }));
      notify(errorText(error));
    }
  }, [notify]);

  useEffect(() => {
    epoch.current += 1;
    setDirsReady(false);
    setResultsByKind({});
    setLoadedKinds({});
    setSelected({});
    setCursor(null);
    setLightbox(null);
    void loadDirs().catch((error) => {
      setDirsReady(true);
      notify(errorText(error));
    });
  }, [dirKey, loadDirs, notify]);

  useEffect(() => {
    if (!dirsReady || loadedKinds[kind]) return;
    void loadResults(kind);
  }, [dirsReady, kind, loadedKinds, loadResults]);

  useEffect(() => {
    if (resumed.current) return;
    resumed.current = true;
    void (async () => {
      const candidates = ['aesthetic', 'wd14', 'wd14_cache'];
      for (const candidate of candidates) {
        const active = await api.findActiveJob(candidate);
        if (!active?.job_id || active.status === 'idle') continue;
        jobMode.current = candidate === 'wd14_cache' ? 'cache' : 'scoring';
        setSnapshot(active as unknown as JobEvent);
        setJobId(active.job_id);
        notify('已恢复任务进度');
        return;
      }
    })().catch((error) => notify(errorText(error)));
  }, [notify]);

  useEffect(() => {
    if (!jobId) return;
    return api.watchJob(jobId, (raw) => {
      const snap = raw as unknown as JobEvent;
      setSnapshot(snap);
      if (snap.status === 'done') {
        setJobId(null);
        if (jobMode.current === 'cache') {
          void loadDirs().catch((error) => notify(errorText(error)));
          notify(snap.message || 'WD14 缓存已就绪');
          return;
        }
        epoch.current += 1;
        setResultsByKind({});
        setLoadedKinds({});
        void loadDirs().catch((error) => notify(errorText(error)));
        notify(snap.message || '评分完成');
      } else if (snap.status === 'cancelled') {
        setJobId(null);
        void loadDirs().catch((error) => notify(errorText(error)));
        notify('已取消');
      } else if (snap.status === 'error') {
        setJobId(null);
        notify('任务失败');
      }
    });
  }, [jobId, loadDirs, notify]);

  useEffect(() => {
    if (!running) return;
    setDirsDisabled(true);
    return () => setDirsDisabled(false);
  }, [running, setDirsDisabled]);

  const kinds = useMemo(
    () => [
      { key: 'aesthetic', label: '美学' },
      ...items.map((item) => ({ key: item.key, label: item.label })),
    ],
    [items],
  );

  const sorted = useMemo(() => {
    const list = results.slice();
    list.sort((a, b) => {
      const left = sortKey === 'path' ? a.path : a.score;
      const right = sortKey === 'path' ? b.path : b.score;
      if (left < right) return sortDesc ? 1 : -1;
      if (left > right) return sortDesc ? -1 : 1;
      return 0;
    });
    return list;
  }, [results, sortDesc, sortKey]);

  const bins = useMemo(
    () => (kind === 'aesthetic' ? null : scoreBins(sorted)),
    [kind, sorted],
  );

  const histogram = useMemo(
    () => buildHistogram(kind, sorted, bins),
    [bins, kind, sorted],
  );
  const histogramMax = useMemo(
    () => Math.max(...histogram.map((bin) => bin.count), 1),
    [histogram],
  );

  const activeFilter = useMemo(
    () =>
      scoreFilter === 'all' || histogram.some((bin) => bin.value === scoreFilter)
        ? scoreFilter
        : 'all',
    [histogram, scoreFilter],
  );

  const visible = useMemo(() => {
    const needle = query.toLowerCase();
    return sorted.filter((result) => {
      if (needle && !result.path.toLowerCase().includes(needle)) return false;
      if (activeFilter === 'all') return true;
      if (kind === 'aesthetic') return String(result.pony ?? 0) === activeFilter;
      if (!bins) return false;
      return String(scoreBin(result.score, bins)) === activeFilter;
    });
  }, [activeFilter, bins, kind, query, sorted]);

  const stats = useMemo(() => computeStats(visible.map((result) => result.score)), [visible]);
  const selectedCount = useMemo(
    () => Object.values(selected).filter(Boolean).length,
    [selected],
  );

  const viewItems = useMemo<ViewItem[]>(
    () =>
      visible.map((result) => ({
        key: result.path,
        ratio: 1,
        src: result.thumb_url,
        fallbackSrc: api.localImageUrl(result.path),
        tooltip: result.path,
        label:
          kind === 'aesthetic'
            ? `score_${result.pony ?? 0} · ${result.score.toFixed(4)} · ${fileName(result.path)}`
            : `${result.score.toFixed(4)} · ${fileName(result.path)}`,
        kind: 'image' as const,
      })),
    [kind, visible],
  );

  /**
   * 切换评分项标签页，并清空搜索与档位筛选。
   *
   * Args:
   *   next (string): 目标评分项 key。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ScoringPage`：标签页按钮点击。
   */
  const switchKind = useCallback(
    (next: string) => {
      if (next === kind) return;
      setKind(next);
      setQuery('');
      setScoreFilter('all');
      setSelected({});
      setCursor(null);
      setLightbox(null);
      persistScoring({ kind: next, filter: 'all' });
    },
    [kind, persistScoring],
  );

  /**
   * 关闭灯箱并按方向在可见结果中循环切换。
   *
   * Args:
   *   delta (number): +1 下一张，-1 上一张。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ScoringPage`：灯箱箭头与键盘事件。
   */
  const stepLightbox = useCallback(
    (delta: number) => {
      setLightbox((current) => {
        if (!current || !visible.length) return null;
        const index = visible.findIndex((result) => result.path === current);
        if (index < 0) return null;
        return visible[(index + delta + visible.length) % visible.length].path;
      });
    },
    [visible],
  );

  const lightboxIndex = lightbox
    ? visible.findIndex((result) => result.path === lightbox)
    : -1;
  const current = lightboxIndex >= 0 ? visible[lightboxIndex] : null;
  useScrollLock(Boolean(current));

  useEffect(() => {
    if (!current) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setLightbox(null);
      } else if (event.key === 'ArrowLeft' || event.key === 'a' || event.key === 'A') {
        stepLightbox(-1);
      } else if (event.key === 'ArrowRight' || event.key === 'd' || event.key === 'D') {
        stepLightbox(1);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
    };
  }, [current, stepLightbox]);

  /**
   * 切换单张卡片选中态；按住 Shift 时从上次锚点连选到当前卡片。
   *
   * Args:
   *   key (string): 图片路径。
   *   shiftKey (boolean): 是否按住 Shift。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `ScoringPage`：Masonry 卡片点击回调。
   */
  const selectCard = useCallback(
    (key: string, shiftKey: boolean) => {
      const keys = visible.map((result) => result.path);
      setSelected((currentSelection) => {
        const target = keys.indexOf(key);
        if (shiftKey && cursor) {
          const from = keys.indexOf(cursor);
          if (from >= 0 && target >= 0) {
            const next = { ...currentSelection };
            for (const path of keys.slice(
              Math.min(from, target),
              Math.max(from, target) + 1,
            )) {
              next[path] = true;
            }
            return next;
          }
        }
        const next = { ...currentSelection };
        if (next[key]) delete next[key];
        else next[key] = true;
        return next;
      });
      setCursor(key);
    },
    [cursor, visible],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a' && !event.shiftKey) {
        event.preventDefault();
        const next: Record<string, boolean> = {};
        for (const result of visible) next[result.path] = true;
        setSelected(next);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [visible]);

  /**
   * 提交评分任务，评分项标签页提交 WD14 评分，美学标签页提交美学评分。
   *
   * Returns:
   *   Promise<void>: 提交成功后进入任务监听。
   *
   * Callers:
   *   - `ScoringPage`：开始按钮。
   */
  const startJob = useCallback(async () => {
    if (!dirs.length) {
      notify('请选择目录');
      return;
    }
    try {
      const result = await request<{ job_id: string }>(apiPath('/scoring/start'), {
        method: 'POST',
        body: JSON.stringify({
          kind: kind === 'aesthetic' ? 'aesthetic' : 'wd14',
          dirs,
        }),
      });
      jobMode.current = 'scoring';
      setSnapshot({ status: 'queued', message: '等待执行', done: 0, total: 0, failed: 0 });
      setJobId(result.job_id);
      notify('已提交');
    } catch (error) {
      notify(errorText(error));
    }
  }, [dirs, kind, notify]);

  /**
   * 为所选目录补齐 WD14 推理缓存。
   *
   * Returns:
   *   Promise<void>: 提交成功后进入任务监听。
   *
   * Callers:
   *   - `ScoringPage`：建立缓存按钮。
   */
  const startCache = useCallback(async () => {
    if (!dirs.length) {
      notify('请选择目录');
      return;
    }
    try {
      const result = await request<{ job_id: string }>(apiPath('/scoring/wd14'), {
        method: 'POST',
        body: JSON.stringify({ dirs }),
      });
      jobMode.current = 'cache';
      setSnapshot({ status: 'queued', message: '等待执行', done: 0, total: 0, failed: 0 });
      setJobId(result.job_id);
      notify('已提交 WD14 缓存任务');
    } catch (error) {
      notify(errorText(error));
    }
  }, [dirs, notify]);

  /**
   * 请求取消当前评分或缓存任务。
   *
   * Returns:
   *   Promise<void>: 取消请求完成后提示。
   *
   * Callers:
   *   - `ScoringPage`：取消按钮。
   */
  const cancelJob = useCallback(async () => {
    if (!jobId) return;
    try {
      await api.cancelJob(jobId);
      notify('正在取消…');
    } catch (error) {
      notify(errorText(error));
    }
  }, [jobId, notify]);

  /**
   * 新增自定义评分项。
   *
   * Returns:
   *   Promise<void>: 提交成功后刷新评分项与数量。
   *
   * Callers:
   *   - `ScoringPage`：评分项对话框添加按钮与表达式回车。
   */
  const addItem = useCallback(async () => {
    const label = itemLabel.trim();
    const expr = itemExpr.trim();
    if (!label || !expr) {
      notify('请填写名称与表达式');
      return;
    }
    try {
      const data = await request<{ items: ScoreItem[] }>(apiPath('/scoring/items'), {
        method: 'POST',
        body: JSON.stringify({ label, expr }),
      });
      setItems(data.items ?? []);
      setItemLabel('');
      setItemExpr('');
      await loadDirs();
      notify('已添加评分项');
    } catch (error) {
      notify(errorText(error));
    }
  }, [itemExpr, itemLabel, loadDirs, notify]);

  /**
   * 删除自定义评分项及其结果缓存。
   *
   * Args:
   *   key (string): 评分项 key。
   *
   * Returns:
   *   Promise<void>: 删除成功后刷新评分项与数量。
   *
   * Callers:
   *   - `ScoringPage`：评分项对话框删除按钮。
   */
  const deleteItem = useCallback(
    async (key: string) => {
      try {
        const data = await request<{ items: ScoreItem[] }>(
          apiPath(`/scoring/items/${encodeURIComponent(key)}`),
          { method: 'DELETE' },
        );
        setItems(data.items ?? []);
        setResultsByKind((currentResults) => {
          const next = { ...currentResults };
          delete next[key];
          return next;
        });
        setLoadedKinds((currentKinds) => {
          const next = { ...currentKinds };
          delete next[key];
          return next;
        });
        setCounts((currentCounts) => {
          const next = { ...currentCounts };
          delete next[key];
          return next;
        });
        if (kind === key) switchKind('aesthetic');
        await loadDirs();
        notify('已删除评分项');
      } catch (error) {
        notify(errorText(error));
      }
    },
    [kind, loadDirs, notify, switchKind],
  );

  useStatus([
    { kind: 'total', value: visible.length },
    ...(selectedCount ? [{ kind: 'selected' as const, value: selectedCount }] : []),
    ...(loadedKinds[kind] && stats.count
      ? [
          {
            kind: 'resource' as const,
            name: '结果',
            value: `${stats.count} 张${stats.mean === null ? '' : ` · 均值 ${stats.mean.toFixed(4)}`}`,
          },
        ]
      : []),
  ]);
  useJob(
    snapshot
      ? {
          snapshot,
          onCancel: () => void cancelJob(),
          cancelling: Boolean(snapshot.cancel_requested),
          onClear: () => setSnapshot(null),
        }
      : null,
  );

  return (
    <div>
      <PageHint>
        Ctrl+A 全选 · 点击图片预览 · ←/A →/D 切换 · Esc 关闭预览
      </PageHint>
      <Toolbar>
        <Button variant="primary" onClick={() => void startJob()} disabled={running}>
          开始
        </Button>
        <Button
          onClick={() => void startCache()}
          disabled={running || !wd14.ready}
          title={
            wd14.ready
              ? `WD14 缓存 ${wd14.count}/${wd14.total} (全部目录); 点击为所选目录补齐`
              : wd14.message || 'WD14 模型未就绪'
          }
        >
          建立缓存
        </Button>

        <ToolbarDivider />

        <ToolbarGroup>
          {kinds.map((entry) => (
            <button
              key={entry.key}
              type="button"
              onClick={() => switchKind(entry.key)}
              className={
                entry.key === kind
                  ? 'flex items-center gap-1.5 border-b-2 border-primary px-3 py-1 text-xs text-foreground'
                  : 'flex items-center gap-1.5 border-b-2 border-transparent px-3 py-1 text-xs text-muted-foreground hover:text-foreground'
              }
            >
              {entry.label}
              <span className="min-w-4 rounded-full bg-white/10 px-1.5 py-px text-center text-xs">
                {counts[entry.key] ?? 0}
              </span>
            </button>
          ))}
        </ToolbarGroup>

        <ToolbarDivider />

        <Button onClick={() => setItemsOpen(true)} title="管理评分项">
          评分项
        </Button>

        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          排序
          <Select
            value={sortKey}
            onChange={(event) => {
              const next = event.target.value === 'path' ? 'path' : 'score';
              setSortKey(next);
              setSortDesc(true);
              persistScoring({ sort: next, desc: true });
            }}
          >
            <option value="score">按分数</option>
            <option value="path">按路径</option>
          </Select>
        </label>
        <Button
          variant="ghost"
          size="icon"
          title="切换升降序"
          onClick={() => {
            const next = !sortDesc;
            setSortDesc(next);
            persistScoring({ desc: next });
          }}
        >
          {sortDesc ? '↓' : '↑'}
        </Button>
        <Input
          type="search"
          className="w-56 max-w-[32vw]"
          placeholder="搜索文件名…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <Select
          value={activeFilter}
          onChange={(event) => {
            setScoreFilter(event.target.value);
            persistScoring({ filter: event.target.value });
          }}
        >
          <option value="all">全部档位</option>
          {histogram.map((bin) => (
            <option key={bin.value} value={bin.value}>
              {bin.label} ({bin.count})
            </option>
          ))}
        </Select>
      </Toolbar>

      {!dirsReady || !loadedKinds[kind] ? (
        <div className="py-16 text-center text-sm text-muted-foreground">加载中…</div>
      ) : null}

      {dirsReady && loadedKinds[kind] && !results.length ? (
        <div className="py-16 text-center text-sm text-muted-foreground">暂无评分数据</div>
      ) : null}

      {dirsReady && loadedKinds[kind] && results.length ? (
        <>
          <div className="grid grid-cols-2 gap-2 border-b border-border bg-white/[0.02] px-3.5 py-2.5 sm:grid-cols-3 lg:grid-cols-6">
            {[
              { label: '均值', value: stats.mean, tone: 'text-green-400' },
              { label: '中位', value: stats.median, tone: 'text-green-400' },
              { label: '标准差', value: stats.std, tone: 'text-muted-foreground' },
              { label: '最低', value: stats.min, tone: 'text-red-400' },
              { label: '最高', value: stats.max, tone: 'text-green-400' },
            ].map((entry) => (
              <div key={entry.label} className="text-center">
                <div className="text-xs uppercase tracking-wide text-muted-foreground">
                  {entry.label}
                </div>
                <div className={`text-sm font-semibold ${entry.tone}`}>
                  {entry.value === null ? '-' : entry.value.toFixed(4)}
                </div>
              </div>
            ))}
            <div className="text-center">
              <div className="text-xs uppercase tracking-wide text-muted-foreground">总数</div>
              <div className="text-sm font-semibold text-muted-foreground">{stats.count}</div>
            </div>
          </div>

          <div className="border-b border-border px-3.5 py-3">
            <div className="flex h-36 items-end justify-center gap-1.5">
              {histogram.map((bin) => (
                <div
                  key={bin.value}
                  className="flex basis-12 flex-col items-center gap-1"
                >
                  <div className="text-xs font-semibold text-foreground/80">
                    {bin.count}
                  </div>
                  <div
                    className="w-7 min-h-1 rounded-t"
                    style={{
                      height: `${Math.round((bin.count / histogramMax) * 120)}px`,
                      background: bin.colour,
                    }}
                  />
                  <div
                    className="whitespace-nowrap text-xs font-medium"
                    style={{ color: bin.colour }}
                  >
                    {bin.label}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <Masonry
            items={viewItems}
            fixedColumns={0}
            selected={selected}
            hasMore={false}
            hasPrev={false}
            loading={false}
            onCardClick={selectCard}
            onCardContext={(key) => setLightbox(key)}
            onLoadMore={() => {}}
          />
        </>
      ) : null}

      {current ? (
        <div
          className="fixed inset-0 z-[80] flex flex-col items-center justify-center bg-black/95"
          onClick={() => setLightbox(null)}
        >
          <span
            className="absolute right-6 top-4 z-10 cursor-pointer text-3xl leading-none text-muted-foreground hover:text-foreground"
            onClick={(event) => {
              event.stopPropagation();
              setLightbox(null);
            }}
          >
            ×
          </span>
          <button
            type="button"
            className="absolute left-3.5 top-1/2 -translate-y-1/2 px-2.5 text-4xl text-muted-foreground hover:text-foreground"
            onClick={(event) => {
              event.stopPropagation();
              stepLightbox(-1);
            }}
          >
            ‹
          </button>
          <button
            type="button"
            className="absolute right-3.5 top-1/2 -translate-y-1/2 px-2.5 text-4xl text-muted-foreground hover:text-foreground"
            onClick={(event) => {
              event.stopPropagation();
              stepLightbox(1);
            }}
          >
            ›
          </button>
          <img
            src={current.image_url}
            alt={current.path}
            className="max-h-[78vh] max-w-[92vw] rounded-lg object-contain shadow-2xl"
            onClick={(event) => event.stopPropagation()}
          />
          <div
            className="mt-3 flex items-center gap-3 text-xs text-muted-foreground"
            onClick={(event) => event.stopPropagation()}
          >
            <span className="rounded-full bg-primary px-2.5 py-0.5 text-xs font-bold text-white">
              {current.score.toFixed(4)}
            </span>
            <span className="text-xs text-muted-foreground">
              {kind === 'aesthetic' ? `score_${current.pony ?? 0}` : kindLabel(kind, items)}
            </span>
            <span className="text-xs text-muted-foreground">{current.path}</span>
          </div>
        </div>
      ) : null}

      <Dialog
        open={itemsOpen}
        onOpenChange={setItemsOpen}
        title="评分项"
        className="max-w-2xl"
        footer={<Button onClick={() => setItemsOpen(false)}>关闭</Button>}
      >
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            {items.map((item) => (
              <div
                key={item.key}
                className="flex items-center gap-2 rounded-md border border-border px-2 py-1.5 text-xs"
              >
                <span className="whitespace-nowrap font-semibold">{item.label}</span>
                <span
                  className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap font-mono text-muted-foreground"
                  title={item.expr}
                >
                  {item.expr}
                </span>
                {item.builtin ? (
                  <span className="text-xs text-muted-foreground">内置</span>
                ) : (
                  <Button variant="danger" size="sm" onClick={() => void deleteItem(item.key)}>
                    删除
                  </Button>
                )}
              </div>
            ))}
          </div>
          <div className="flex flex-col gap-1.5 border-t border-border pt-2.5">
            <Input
              value={itemLabel}
              onChange={(event) => setItemLabel(event.target.value)}
              placeholder="名称, 如 CSAM"
              spellCheck={false}
            />
            <Input
              value={itemExpr}
              onChange={(event) => setItemExpr(event.target.value)}
              placeholder='表达式, 如 max("loli", "shota")'
              spellCheck={false}
              onKeyDown={(event) => {
                if (event.key !== 'Enter') return;
                event.preventDefault();
                void addItem();
              }}
            />
            <div className="text-xs leading-relaxed text-muted-foreground">
              双引号字符串引用同名 WD14 标签概率; 语法同蒙板表达式: 算术、比较与 min / max /
              sum / avg / abs / sqrt / exp / log / pow / clamp / blur.
            </div>
            <div>
              <Button variant="primary" onClick={() => void addItem()}>
                添加
              </Button>
            </div>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
