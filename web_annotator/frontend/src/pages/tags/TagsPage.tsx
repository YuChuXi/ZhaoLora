import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { BatchSnapshot } from '@/lib/types';
import { api, apiPath, request } from '@/lib/api';
import { useApp } from '@/app/AppContext';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/controls';
import { Dialog } from '@/components/ui/dialog';
import { Toolbar, ToolbarDivider } from '@/components/ui/toolbar';
import { useJob, useStatus } from '@/components/ui/status';

/** 标签频率统计的一行。 */
interface TagRow {
  tag: string;
  count: number;
  pct: number;
  zh: string;
}

/** 标签频率统计响应。 */
interface TagStatsResult {
  sample_count: number;
  skipped: number;
  tag_count: number;
  tags: TagRow[];
}

/** 标签统计条件（已应用的取值）。 */
interface StatsState {
  lower: boolean;
  underscores: boolean;
  perSample: boolean;
  minCount: number;
  section: string;
  include: string[];
  exclude: string[];
  matchMode: string;
}

/** 标签统计条件对话框的草稿取值，输入框内容保持字符串。 */
interface StatsDraft {
  lower: boolean;
  underscores: boolean;
  perSample: boolean;
  minCount: string;
  section: string;
  include: string;
  exclude: string;
  matchMode: string;
}

/** 批量添加对话框的持久化参数。 */
interface BatchAddSettings {
  separator_mode: string;
  position: string;
  insert_mode: string;
  conditional: boolean;
  keep_duplicates: boolean;
  match_mode: string;
  inject_pony_score: boolean;
  inject_year_tag: boolean;
}

/** 标签批量编辑的持久化参数。 */
interface BatchSettings {
  add?: BatchAddSettings;
  delete_match_mode?: string;
  replace_match_mode?: string;
}

/** 批量添加对话框状态。 */
interface AddState {
  tags: string;
  separator: string;
  position: string;
  insertMode: string;
  conditional: boolean;
  keepDup: boolean;
  matchMode: string;
  injectPony: boolean;
  injectYear: boolean;
  result: string;
  busy: boolean;
}

/** 批量删除对话框状态。 */
interface DeleteState {
  tags: string;
  matchMode: string;
  result: string;
  busy: boolean;
}

/** 批量替换对话框状态。 */
interface ReplaceState {
  from: string;
  to: string;
  matchMode: string;
  result: string;
  busy: boolean;
}

/** 标签批量编辑任务结果。 */
interface TagCleanResult {
  changed: number;
  unchanged: number;
  inject_year_tag?: boolean;
  year_injected?: number;
  year_missing?: number;
  matched_tags?: Record<string, string[]>;
  samples?: Array<{ path: string; name?: string; before: string; after: string }>;
}

/** 后端长任务快照中标签/打标页面用到的字段。 */
type JobSnapshot = BatchSnapshot & {
  status: string;
  done?: number;
  total?: number;
  failed?: number;
  skipped?: number;
  pct?: number;
  phase?: string;
  cancel_requested?: boolean;
  logs?: string[];
  result?: TagCleanResult | null;
};

/** 打标设置。 */
interface TaggingSettings {
  threshold?: number;
  disable_orig_tags?: boolean;
  force?: boolean;
  artist_tag?: string;
}

/** 打标页目录与模型状态响应。 */
interface TaggingDirsData {
  model_ready: boolean;
  model_path: string;
  default_threshold: number;
}

/** 任务清单中的触发词条目。 */
interface TaskEntry {
  name: string;
  trigger_word: string;
}

type BatchMode = 'add' | 'delete' | 'replace';

/**
 * 提取错误对象的可读文本。
 *
 * Args:
 *   error (unknown): 捕获到的异常。
 *
 * Returns:
 *   string: 异常消息或字符串化结果。
 *
 * Callers:
 *   - `TagsPage` 及其各对话框的错误提示。
 */
function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 按逗号或换行拆分标签列表并去除空白项。
 *
 * Args:
 *   raw (string): 用户输入的标签文本。
 *
 * Returns:
 *   string[]: 拆分后的标签列表。
 *
 * Callers:
 *   - `TagsPage.applyStatsOptions`
 *   - `TagsPage.runBatchEdit`
 */
function parseTagList(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * 过滤出设置中的字符串数组。
 *
 * Args:
 *   value (unknown): 任意设置值。
 *
 * Returns:
 *   string[]: 仅包含字符串元素的数组，非数组输入返回空数组。
 *
 * Callers:
 *   - `readStatsSettings`
 */
function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

/**
 * 从设置对象读取标签统计条件，缺失字段使用页面默认值。
 *
 * Args:
 *   raw (unknown): `tag_stats` 设置值。
 *
 * Returns:
 *   StatsState: 规整后的统计条件。
 *
 * Callers:
 *   - `TagsPage` 初始化统计条件。
 */
function readStatsSettings(raw: unknown): StatsState {
  const saved = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  return {
    lower: saved.lower !== false,
    underscores: saved.underscores !== false,
    perSample: saved.per_sample !== false,
    minCount: typeof saved.min_count === 'number' && saved.min_count >= 1 ? saved.min_count : 1,
    section: typeof saved.section === 'string' ? saved.section : 'all',
    include: stringList(saved.include),
    exclude: stringList(saved.exclude),
    matchMode: typeof saved.match_mode === 'string' ? saved.match_mode : 'exact',
  };
}

/**
 * 从设置对象读取批量编辑参数。
 *
 * Args:
 *   raw (unknown): `tag_batch` 设置值。
 *
 * Returns:
 *   BatchSettings: 规整后的批量编辑参数。
 *
 * Callers:
 *   - `TagsPage` 初始化批量对话框。
 */
function readBatchSettings(raw: unknown): BatchSettings {
  const saved = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  return {
    add: saved.add && typeof saved.add === 'object' ? (saved.add as BatchAddSettings) : undefined,
    delete_match_mode:
      typeof saved.delete_match_mode === 'string' ? saved.delete_match_mode : undefined,
    replace_match_mode:
      typeof saved.replace_match_mode === 'string' ? saved.replace_match_mode : undefined,
  };
}

/**
 * 将已应用的统计条件转换为对话框草稿。
 *
 * Args:
 *   value (StatsState): 已应用的统计条件。
 *
 * Returns:
 *   StatsDraft: 适用于输入框的草稿对象。
 *
 * Callers:
 *   - `TagsPage` 打开统计条件对话框。
 */
function toStatsDraft(value: StatsState): StatsDraft {
  return {
    lower: value.lower,
    underscores: value.underscores,
    perSample: value.perSample,
    minCount: String(value.minCount),
    section: value.section,
    include: value.include.join(', '),
    exclude: value.exclude.join(', '),
    matchMode: value.matchMode,
  };
}

/**
 * 将标签批量编辑任务结果整理为预览文本。
 *
 * Args:
 *   data (TagCleanResult): 任务终态结果。
 *   dryRun (boolean): 是否预览模式。
 *
 * Returns:
 *   string: 逐行展示的结果文本。
 *
 * Callers:
 *   - `TagsPage.runBatchEdit`
 */
function batchResultLines(data: TagCleanResult, dryRun: boolean): string {
  const lines: string[] = [];
  lines.push(
    `${dryRun ? '预览完成' : '写入完成'} · 已变更 ${data.changed} · 未变更 ${data.unchanged}`,
  );
  if (data.inject_year_tag) {
    lines.push(
      `年份标签: 已注入 ${data.year_injected || 0} · 无发布时间 ${data.year_missing || 0}`,
    );
  }
  const matched = data.matched_tags || {};
  const matchedKeys = Object.keys(matched).filter((key) => (matched[key] || []).length);
  if (matchedKeys.length) {
    lines.push('匹配命中:');
    for (const pattern of matchedKeys) {
      lines.push(`  ${pattern} → ${matched[pattern].join(', ')}`);
    }
  }
  for (const sample of data.samples || []) {
    lines.push(`--- ${sample.name || sample.path}`);
    lines.push(`- ${sample.before}`);
    lines.push(`+ ${sample.after}`);
  }
  return lines.join('\n');
}

/**
 * 等待标签批量编辑任务结束并按进度消息回显状态。
 *
 * Args:
 *   jobId (string): 任务标识。
 *   onMessage (function): 排队与运行阶段的状态文本回调。
 *   runningText (string): 运行阶段无消息时的占位文本。
 *
 * Returns:
 *   Promise<TagCleanResult>: 任务终态结果。
 *
 * Callers:
 *   - `TagsPage.runBatchEdit`
 */
function watchBatchJob(
  jobId: string,
  onMessage: (text: string) => void,
  runningText: string,
): Promise<TagCleanResult> {
  return new Promise<TagCleanResult>((resolve, reject) => {
    api.watchJob(jobId, (raw) => {
      const snap = raw as JobSnapshot;
      if (snap.status === 'queued') {
        onMessage('等待执行…');
        return;
      }
      if (snap.status === 'running') {
        onMessage(snap.message || runningText);
        return;
      }
      if (snap.status === 'done') {
        resolve((snap.result as TagCleanResult) ?? { changed: 0, unchanged: 0 });
        return;
      }
      if (snap.status === 'error' || snap.status === 'cancelled') {
        reject(new Error(snap.message || snap.status));
      }
    });
  });
}

/**
 * 复制文本到剪贴板；剪贴板接口不可用时改用 execCommand 并选中导出框。
 *
 * Args:
 *   text (string): 待复制文本。
 *   textarea (HTMLTextAreaElement | null): 页面导出框。
 *   success (string): 复制成功后的提示文本。
 *   notify (function): 全局提示函数。
 *
 * Returns:
 *   Promise<void>: 复制流程完成。
 *
 * Callers:
 *   - `TagsPage.copyExport`
 *   - `TagsPage.exportList`
 */
async function copyText(
  text: string,
  textarea: HTMLTextAreaElement | null,
  success: string,
  notify: (message: string) => void,
): Promise<void> {
  if (!text) {
    notify('无可复制内容');
    return;
  }
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      notify(success);
      return;
    } catch {
      // 非安全上下文下剪贴板接口不可用，转入 execCommand 回退。
    }
  }
  if (!textarea) {
    notify('剪贴板不可用，请手动复制');
    return;
  }
  textarea.value = text;
  textarea.focus();
  textarea.select();
  if (document.execCommand('copy')) {
    notify(success);
    return;
  }
  notify('剪贴板不可用，已选中文本，请按 Ctrl+C');
}

/**
 * 标签页：频率统计、批量编辑、自动生成与触发词维护。
 *
 * Args:
 *   无。
 *
 * Returns:
 *   JSX.Element: 标签页视图。
 *
 * Callers:
 *   - 前端页面注册表按 page 名 `tags` 挂载。
 */
export function TagsPage() {
  const app = useApp();
  const { dirs, notify, saveSettings, dataset, setDirsDisabled } = app;
  const savedStats = useMemo(() => readStatsSettings(app.settings.tag_stats), [app.settings]);
  const savedBatch = useMemo(() => readBatchSettings(app.settings.tag_batch), [app.settings]);

  const [stats, setStats] = useState<StatsState>(savedStats);
  const [draft, setDraft] = useState<StatsDraft>(() => toStatsDraft(savedStats));
  const [tags, setTags] = useState<TagRow[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [triggers, setTriggers] = useState<Record<string, boolean>>({});
  const [summary, setSummary] = useState('-');
  const [loading, setLoading] = useState(false);
  const [translating, setTranslating] = useState(false);
  const [tableError, setTableError] = useState<string | null>(null);
  const [exportText, setExportText] = useState('');
  const exportRef = useRef<HTMLTextAreaElement>(null);
  const headRef = useRef<HTMLInputElement>(null);

  const [statsOpen, setStatsOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [taggingOpen, setTaggingOpen] = useState(false);
  const [taggingRunning, setTaggingRunning] = useState(false);

  const [add, setAdd] = useState<AddState>(() => ({
    tags: '',
    separator: savedBatch.add?.separator_mode ?? 'keep',
    position: savedBatch.add?.position ?? 'before',
    insertMode: savedBatch.add?.insert_mode ?? 'top',
    conditional: !!savedBatch.add?.conditional,
    keepDup: !!savedBatch.add?.keep_duplicates,
    matchMode: savedBatch.add?.match_mode ?? 'exact',
    injectPony: !!savedBatch.add?.inject_pony_score,
    injectYear: !!savedBatch.add?.inject_year_tag,
    result: '',
    busy: false,
  }));
  const [del, setDel] = useState<DeleteState>(() => ({
    tags: '',
    matchMode: savedBatch.delete_match_mode ?? 'exact',
    result: '',
    busy: false,
  }));
  const [replace, setReplace] = useState<ReplaceState>(() => ({
    from: '',
    to: '',
    matchMode: savedBatch.replace_match_mode ?? 'exact',
    result: '',
    busy: false,
  }));
  const addSaved = useRef(!!savedBatch.add);

  const selectedTags = useMemo(
    () => tags.filter((row) => !!selected[row.tag]),
    [tags, selected],
  );

  useEffect(() => {
    if (!taggingRunning) return;
    setDirsDisabled(true);
    return () => setDirsDisabled(false);
  }, [taggingRunning, setDirsDisabled]);

  useEffect(() => {
    setExportText(selectedTags.map((row) => row.tag).join(', '));
  }, [selectedTags]);

  useEffect(() => {
    const head = headRef.current;
    if (!head) return;
    const all = tags.length > 0 && selectedTags.length === tags.length;
    head.checked = all;
    head.indeterminate = selectedTags.length > 0 && selectedTags.length < tags.length;
  }, [tags.length, selectedTags.length]);

  /**
   * 拉取当前目录的标签频率统计。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 统计完成。
   *
   * Callers:
   *   - `TagsPage` 的目录/条件变化副作用
   *   - `TagsPage` 工具栏「统计」
   *   - `TagsPage` 统计条件「确定」
   *   - `TagsPage` 翻译缺失完成
   *   - `TagsPage` 打标完成
   */
  const refresh = useCallback(async () => {
    if (!dirs.length) {
      notify('请选择目录');
      return;
    }
    setLoading(true);
    setTableError(null);
    saveSettings({
      tag_stats: {
        lower: stats.lower,
        underscores: stats.underscores,
        per_sample: stats.perSample,
        min_count: stats.minCount,
        section: stats.section,
        include: stats.include,
        exclude: stats.exclude,
        match_mode: stats.matchMode,
      },
    });
    try {
      const data = await request<TagStatsResult>(apiPath('/tags/stats'), {
        method: 'POST',
        body: JSON.stringify({
          dirs,
          lower: stats.lower,
          underscores: stats.underscores,
          per_sample: stats.perSample,
          min_count: stats.minCount,
          section: stats.section,
          include: stats.include,
          exclude: stats.exclude,
          match_mode: stats.matchMode,
        }),
      });
      const rows = data.tags ?? [];
      setTags(rows);
      setSelected((current) => {
        const next: Record<string, boolean> = {};
        for (const row of rows) if (current[row.tag]) next[row.tag] = true;
        return next;
      });
      setSummary(
        `样本 ${data.sample_count} · 标签 ${data.tag_count}` +
          (data.skipped ? ` · 排除 ${data.skipped}` : ''),
      );
    } catch (error) {
      setTableError(messageOf(error));
    } finally {
      setLoading(false);
    }
  }, [dirs, notify, saveSettings, stats]);

  /**
   * 读取当前数据集的触发词集合并回显到勾选状态。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 触发词加载完成。
   *
   * Callers:
   *   - `TagsPage` 启动副作用
   *   - `TagsPage` 打标完成回调
   */
  const loadTriggers = useCallback(async () => {
    const result = await request<{ tasks: TaskEntry[] }>('/api/tasks');
    const task = (result.tasks || []).find((item) => item.name === dataset);
    const next: Record<string, boolean> = {};
    for (const part of (task?.trigger_word || '').split(',')) {
      const word = part.trim();
      if (word) next[word] = true;
    }
    setTriggers(next);
  }, [dataset]);

  useEffect(() => {
    void loadTriggers().catch((error) =>
      notify(`触发词加载失败: ${messageOf(error)}`),
    );
  }, [loadTriggers, notify]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleTaggingDone = useCallback(() => {
    void loadTriggers()
      .then(() => refresh())
      .catch((error) => notify(messageOf(error)));
  }, [loadTriggers, notify, refresh]);

  /**
   * 切换单个标签的选中状态。
   *
   * Args:
   *   tag (string): 标签名。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `TagsPage` 表格行点击与行勾选框。
   */
  const toggleTag = (tag: string) => {
    setSelected((current) => {
      const next = { ...current };
      if (next[tag]) delete next[tag];
      else next[tag] = true;
      return next;
    });
  };

  /**
   * 全选或清空当前统计结果中的标签。
   *
   * Args:
   *   checked (boolean): 是否全选。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `TagsPage` 表头勾选框。
   */
  const toggleAll = (checked: boolean) => {
    if (!checked) {
      setSelected({});
      return;
    }
    const next: Record<string, boolean> = {};
    for (const row of tags) next[row.tag] = true;
    setSelected(next);
  };

  /**
   * 按勾选状态增量写入或移除任务触发词。
   *
   * Args:
   *   tag (string): 标签名。
   *   checked (boolean): 勾选后写入，取消后移除。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `TagsPage` 表格触发词勾选框。
   */
  const toggleTrigger = (tag: string, checked: boolean) => {
    const suffix = checked ? 'add' : 'remove';
    void request(`/api/tasks/${encodeURIComponent(dataset)}/trigger-word/${suffix}`, {
      method: 'POST',
      body: JSON.stringify({ trigger_word: tag }),
    }).catch((error) => notify(`触发词保存失败: ${messageOf(error)}`));
    setTriggers((current) => {
      const next = { ...current };
      if (checked) next[tag] = true;
      else delete next[tag];
      return next;
    });
  };

  /**
   * 复制导出框当前内容。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 复制完成。
   *
   * Callers:
   *   - `TagsPage` 导出框「复制」按钮。
   */
  const copyExport = useCallback(async () => {
    await copyText(exportText, exportRef.current, '已复制', notify);
  }, [exportText, notify]);

  /**
   * 按勾选标签导出英文或中文列表并复制。
   *
   * Args:
   *   field ('en' | 'zh'): 导出字段。
   *
   * Returns:
   *   Promise<void>: 复制完成。
   *
   * Callers:
   *   - `TagsPage` 工具栏「导出英文」/「导出中文」。
   */
  const exportList = useCallback(
    async (field: 'en' | 'zh') => {
      if (!selectedTags.length) {
        notify('请先勾选');
        return;
      }
      const parts = selectedTags.map((row) => (field === 'zh' ? row.zh || row.tag : row.tag));
      const text = parts.join(', ');
      setExportText(text);
      await copyText(
        text,
        exportRef.current,
        `已复制 ${text.split(',').filter(Boolean).length} 项`,
        notify,
      );
    },
    [notify, selectedTags],
  );

  /**
   * 将当前表格中缺少中文的标签提交批量翻译并刷新。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 翻译与刷新完成。
   *
   * Callers:
   *   - `TagsPage` 工具栏「翻译缺失」。
   */
  const translateMissing = async () => {
    const missing = tags.filter((row) => !row.zh).map((row) => row.tag);
    if (!missing.length) {
      notify('没有需要翻译的标签');
      return;
    }
    setTranslating(true);
    try {
      const result = await request<{ translated: number }>(apiPath('/tags/translate-missing'), {
        method: 'POST',
        body: JSON.stringify({ tags: missing }),
      });
      notify(`已翻译 ${result.translated} 个标签`);
      await refresh();
    } catch (error) {
      notify(messageOf(error));
    } finally {
      setTranslating(false);
    }
  };

  /**
   * 打开统计条件对话框并同步当前已应用条件。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `TagsPage` 工具栏「统计条件」。
   */
  const openStatsOptions = () => {
    setDraft(toStatsDraft(stats));
    setStatsOpen(true);
  };

  /**
   * 应用统计条件草稿，触发统计刷新。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `TagsPage` 统计条件对话框「确定」。
   */
  const applyStatsOptions = () => {
    setStats({
      lower: draft.lower,
      underscores: draft.underscores,
      perSample: draft.perSample,
      minCount: Math.max(1, Number.parseInt(draft.minCount, 10) || 1),
      section: draft.section,
      include: parseTagList(draft.include),
      exclude: parseTagList(draft.exclude),
      matchMode: draft.matchMode,
    });
    setStatsOpen(false);
  };

  /**
   * 打开批量添加对话框，并以已勾选标签填充输入框。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `TagsPage` 工具栏「批量添加」。
   */
  const openBatchAdd = () => {
    const picked = selectedTags.map((row) => row.tag).join(', ');
    setAdd((current) => {
      const next: AddState = { ...current, tags: picked, result: '' };
      if (picked && !addSaved.current) {
        next.position = 'before';
        next.insertMode = 'bottom';
        next.conditional = true;
      }
      return next;
    });
    setAddOpen(true);
  };

  /**
   * 打开批量删除对话框，并以已勾选标签填充输入框。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `TagsPage` 工具栏「批量删除」。
   */
  const openBatchDelete = () => {
    setDel((current) => ({
      ...current,
      tags: selectedTags.map((row) => row.tag).join(', '),
      result: '',
    }));
    setDeleteOpen(true);
  };

  /**
   * 打开批量替换对话框，并以已勾选标签填充原标签输入框。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - `TagsPage` 工具栏「批量替换」。
   */
  const openBatchReplace = () => {
    setReplace((current) => ({
      ...current,
      from: selectedTags.map((row) => row.tag).join(', '),
      to: '',
      result: '',
    }));
    setReplaceOpen(true);
  };

  /**
   * 提交标签批量添加、删除或替换任务，预览/写入并回显结果。
   *
   * Args:
   *   mode (BatchMode): 操作类型。
   *   dryRun (boolean): 是否仅预览。
   *
   * Returns:
   *   Promise<void>: 任务与刷新完成。
   *
   * Callers:
   *   - `TagsPage` 三个批量对话框的「预览」/「写入」按钮。
   */
  const runBatchEdit = async (mode: BatchMode, dryRun: boolean) => {
    if (!dirs.length) {
      notify('请选择目录');
      return;
    }
    const tagsInput = mode === 'add' ? add.tags : mode === 'delete' ? del.tags : replace.from;
    const tagList = parseTagList(tagsInput);
    const metaInject =
      mode === 'add' && (add.injectPony || add.injectYear);
    if (!tagList.length && !metaInject) {
      notify('请指定标签');
      return;
    }
    let replacePairs: string[][] = [];
    if (mode === 'replace') {
      const toTags = parseTagList(replace.to);
      if (toTags.length !== tagList.length) {
        notify('原标签与新标签数量须一致');
        return;
      }
      replacePairs = tagList.map((tag, index) => [tag, toTags[index]]);
    }
    if (!dryRun && !window.confirm('将覆盖所选目录中的旁路 .txt，是否继续？')) return;

    const matchMode = mode === 'add' ? add.matchMode : mode === 'delete' ? del.matchMode : replace.matchMode;
    const body = {
      dirs,
      delete_tags: mode === 'delete' ? tagList : [],
      add_tags: mode === 'add' ? tagList : [],
      replace_pairs: mode === 'replace' ? replacePairs : [],
      match_mode: matchMode,
      position: mode === 'add' ? add.position : 'after',
      insert_mode: mode === 'add' ? add.insertMode : 'bottom',
      conditional: mode === 'add' ? add.conditional : false,
      keep_duplicates: mode === 'add' ? add.keepDup : false,
      inject_pony_score: mode === 'add' ? add.injectPony : false,
      inject_year_tag: mode === 'add' ? add.injectYear : false,
      separator_mode: mode === 'add' ? add.separator : 'keep',
      dry_run: dryRun,
      sample_limit: 40,
    };

    const saved = readBatchSettings(app.settings.tag_batch);
    if (mode === 'add') {
      saved.add = {
        separator_mode: add.separator,
        position: add.position,
        insert_mode: add.insertMode,
        conditional: add.conditional,
        keep_duplicates: add.keepDup,
        match_mode: matchMode,
        inject_pony_score: add.injectPony,
        inject_year_tag: add.injectYear,
      };
    } else if (mode === 'delete') {
      saved.delete_match_mode = matchMode;
    } else {
      saved.replace_match_mode = matchMode;
    }
    saveSettings({ tag_batch: saved });

    const runningText = dryRun ? '预览中…' : '写入中…';
    /**
     * 将状态文本写入当前操作对应的结果框。
     *
     * Args:
     *   text (string): 状态或结果文本。
     *
     * Returns:
     *   void
     *
     * Callers:
     *   - `TagsPage.runBatchEdit`
     */
    const setResult = (text: string) => {
      if (mode === 'add') setAdd((current) => ({ ...current, result: text }));
      else if (mode === 'delete') setDel((current) => ({ ...current, result: text }));
      else setReplace((current) => ({ ...current, result: text }));
    };
    /**
     * 设置当前操作对应对话框的忙碌状态。
     *
     * Args:
     *   value (boolean): 是否忙碌。
     *
     * Returns:
     *   void
     *
     * Callers:
     *   - `TagsPage.runBatchEdit`
     */
    const setBusy = (value: boolean) => {
      if (mode === 'add') setAdd((current) => ({ ...current, busy: value }));
      else if (mode === 'delete') setDel((current) => ({ ...current, busy: value }));
      else setReplace((current) => ({ ...current, busy: value }));
    };

    setResult(runningText);
    setBusy(true);
    try {
      const submitted = await request<{ job_id: string }>(apiPath('/tags/batch-edit'), {
        method: 'POST',
        body: JSON.stringify(body),
      });
      const data = await watchBatchJob(submitted.job_id, setResult, runningText);
      setResult(batchResultLines(data, dryRun));
      notify(`${dryRun ? '预览' : '写入'}完成 · 已变更 ${data.changed}`);
      if (!dryRun) await refresh();
    } catch (error) {
      notify(messageOf(error));
    } finally {
      setBusy(false);
    }
  };

  useStatus([{ kind: 'selected', value: selectedTags.length }], summary);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar>
        <Button variant="primary" onClick={() => setTaggingOpen(true)}>
          {taggingRunning ? '自动生成…' : '生成标签'}
        </Button>
        <Button onClick={openBatchAdd}>批量添加</Button>
        <Button onClick={openBatchReplace}>批量替换</Button>
        <Button onClick={openBatchDelete}>批量删除</Button>
        <ToolbarDivider />
        <Button onClick={openStatsOptions}>统计条件</Button>
        <Button onClick={() => void refresh()}>统计</Button>
        <Button disabled={translating} onClick={() => void translateMissing()}>
          {translating ? '翻译中…' : '翻译缺失'}
        </Button>
        <ToolbarDivider />
        <Button onClick={() => void exportList('en')}>导出英文</Button>
        <Button onClick={() => void exportList('zh')}>导出中文</Button>
      </Toolbar>

      <div className="min-h-0 flex-1 overflow-auto px-3 pb-5 pt-2">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              <th className="sticky top-0 z-10 w-9 border-b border-border bg-background px-2.5 py-2 text-left font-semibold text-muted-foreground">
                <input
                  ref={headRef}
                  type="checkbox"
                  title="全选列表"
                  className="h-3.5 w-3.5 accent-[#e94560]"
                  onChange={(event) => toggleAll(event.target.checked)}
                />
              </th>
              <th className="sticky top-0 z-10 whitespace-nowrap border-b border-border bg-background px-2.5 py-2 text-left font-semibold text-muted-foreground">
                占比
              </th>
              <th className="sticky top-0 z-10 w-16 border-b border-border bg-background px-2.5 py-2 text-left font-semibold text-muted-foreground">
                次数
              </th>
              <th className="sticky top-0 z-10 w-2/5 border-b border-border bg-background px-2.5 py-2 text-left font-semibold text-muted-foreground">
                英文
              </th>
              <th className="sticky top-0 z-10 border-b border-border bg-background px-2.5 py-2 text-left font-semibold text-muted-foreground">
                中文
              </th>
              <th className="sticky top-0 z-10 whitespace-nowrap border-b border-border bg-background px-2.5 py-2 text-left font-semibold text-muted-foreground">
                触发词
              </th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={6} className="px-2.5 py-10 text-center text-muted-foreground">
                  统计中…
                </td>
              </tr>
            ) : tableError ? (
              <tr>
                <td colSpan={6} className="px-2.5 py-10 text-center text-muted-foreground">
                  {tableError}
                </td>
              </tr>
            ) : tags.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-2.5 py-10 text-center text-muted-foreground">
                  无结果
                </td>
              </tr>
            ) : (
              tags.map((row) => {
                const checked = !!selected[row.tag];
                return (
                  <tr
                    key={row.tag}
                    className={checked ? 'cursor-pointer bg-primary/15' : 'cursor-pointer'}
                    onClick={() => toggleTag(row.tag)}
                  >
                    <td className="border-b border-border/60 px-2.5 py-1.5">
                      <span onClick={(event) => event.stopPropagation()}>
                        <Checkbox
                          checked={checked}
                          onCheckedChange={() => toggleTag(row.tag)}
                        />
                      </span>
                    </td>
                    <td className="border-b border-border/60 px-2.5 py-1.5 tabular-nums text-[#5eb8ff]">
                      {row.pct.toFixed(2)}%
                    </td>
                    <td className="border-b border-border/60 px-2.5 py-1.5 tabular-nums text-muted-foreground">
                      {row.count}
                    </td>
                    <td className="border-b border-border/60 px-2.5 py-1.5 font-mono">{row.tag}</td>
                    <td
                      className={
                        row.zh
                          ? 'border-b border-border/60 px-2.5 py-1.5 text-[#e8d5b7]'
                          : 'border-b border-border/60 px-2.5 py-1.5 text-muted-foreground opacity-55'
                      }
                    >
                      {row.zh || '—'}
                    </td>
                    <td className="border-b border-border/60 px-2.5 py-1.5">
                      <span
                        title="勾选后实时保存为任务触发词"
                        onClick={(event) => event.stopPropagation()}
                      >
                        <Checkbox
                          checked={!!triggers[row.tag]}
                          onCheckedChange={(value) => toggleTrigger(row.tag, value)}
                        />
                      </span>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <div className="flex shrink-0 items-start gap-2 border-t border-border bg-[#16213e] px-3 py-2">
        <textarea
          ref={exportRef}
          className="min-h-14 max-h-32 flex-1 resize-y rounded-md border border-border bg-[#0a1a30] px-2 py-1 font-mono text-xs outline-none focus:border-[#4a6a9a]"
          readOnly
          placeholder="已选标签"
          value={exportText}
        />
        <Button onClick={() => void copyExport()}>复制</Button>
      </div>

      <Dialog
        open={statsOpen}
        onOpenChange={setStatsOpen}
        title="统计条件"
        className="max-h-[84vh] max-w-md"
        footer={
          <>
            <Button variant="primary" onClick={applyStatsOptions}>
              确定
            </Button>
            <Button onClick={() => setStatsOpen(false)}>关闭</Button>
          </>
        }
      >
        <div className="flex flex-col gap-3 text-xs text-muted-foreground">
          <div className="flex flex-col gap-1.5">
            <label className="flex cursor-pointer items-center gap-1.5">
              <Checkbox
                checked={draft.lower}
                onCheckedChange={(value) => setDraft((current) => ({ ...current, lower: value }))}
              />
              忽略大小写
            </label>
            <label className="flex cursor-pointer items-center gap-1.5">
              <Checkbox
                checked={draft.underscores}
                onCheckedChange={(value) =>
                  setDraft((current) => ({ ...current, underscores: value }))
                }
              />
              统一空格
            </label>
            <label className="flex cursor-pointer items-center gap-1.5">
              <Checkbox
                checked={draft.perSample}
                onCheckedChange={(value) =>
                  setDraft((current) => ({ ...current, perSample: value }))
                }
              />
              按图去重
            </label>
          </div>
          <label className="flex items-center gap-1.5">
            最少出现
            <Input
              className="w-16"
              type="number"
              min={1}
              value={draft.minCount}
              onChange={(event) =>
                setDraft((current) => ({ ...current, minCount: event.target.value }))
              }
            />
          </label>
          <label className="flex items-center gap-1.5">
            统计范围
            <Select
              value={draft.section}
              onChange={(event) =>
                setDraft((current) => ({ ...current, section: event.target.value }))
              }
            >
              <option value="all">全部</option>
              <option value="before">||| 前</option>
              <option value="after">||| 后</option>
            </Select>
          </label>
          <div className="flex flex-col gap-1.5 border-t border-border pt-2.5">
            <label className="flex items-center gap-1.5">
              样本筛选匹配方式
              <Select
                value={draft.matchMode}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, matchMode: event.target.value }))
                }
              >
                <option value="exact">精确</option>
                <option value="danbooru">Danbooru 通配(*)</option>
                <option value="regex">正则</option>
              </Select>
            </label>
            <label className="flex flex-col gap-1">
              仅含标签（逗号分隔; 留空表示不限）
              <Input
                type="search"
                autoComplete="off"
                placeholder="tag1, tag2"
                value={draft.include}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, include: event.target.value }))
                }
              />
            </label>
            <label className="flex flex-col gap-1">
              排除标签（逗号分隔; 留空表示不限）
              <Input
                type="search"
                autoComplete="off"
                placeholder="tag1, tag2"
                value={draft.exclude}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, exclude: event.target.value }))
                }
              />
            </label>
            <span className="opacity-75">通配: * 匹配任意字符；正则作用于归一化标签(小写无下划线)</span>
          </div>
        </div>
      </Dialog>

      <Dialog
        open={addOpen}
        onOpenChange={setAddOpen}
        title="批量添加标签"
        className="max-h-[80vh]"
        footer={
          <>
            <Button disabled={add.busy} onClick={() => void runBatchEdit('add', true)}>
              预览
            </Button>
            <Button variant="danger" disabled={add.busy} onClick={() => void runBatchEdit('add', false)}>
              写入
            </Button>
            <Button onClick={() => setAddOpen(false)}>关闭</Button>
          </>
        }
      >
        <div className="flex flex-col gap-2.5">
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            要添加的标签（逗号分隔）
            <Input
              placeholder="tag1, tag2"
              value={add.tags}
              onChange={(event) =>
                setAdd((current) => ({ ...current, tags: event.target.value }))
              }
            />
          </label>
          <div className="flex flex-col gap-2 text-xs text-muted-foreground">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
              <span>分隔符</span>
              <Select
                value={add.separator}
                onChange={(event) =>
                  setAdd((current) => ({ ...current, separator: event.target.value }))
                }
              >
                <option value="keep">保留 |||</option>
                <option value="add">添加 |||</option>
                <option value="remove">删除 |||</option>
              </Select>
              <span>位置</span>
              <Select
                value={add.position}
                disabled={add.separator === 'remove'}
                onChange={(event) =>
                  setAdd((current) => ({ ...current, position: event.target.value }))
                }
              >
                <option value="before">||| 之前</option>
                <option value="after">||| 之后</option>
              </Select>
              <span>方式</span>
              <Select
                value={add.insertMode}
                onChange={(event) =>
                  setAdd((current) => ({ ...current, insertMode: event.target.value }))
                }
              >
                <option value="top">置顶</option>
                <option value="bottom">置底</option>
                <option value="random">随机</option>
              </Select>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
              <label className="flex cursor-pointer items-center gap-1">
                <Checkbox
                  checked={add.conditional}
                  onCheckedChange={(value) =>
                    setAdd((current) => ({ ...current, conditional: value }))
                  }
                />
                仅当已有匹配标签时才插入
              </label>
              <label className="flex cursor-pointer items-center gap-1">
                <Checkbox
                  checked={add.keepDup}
                  onCheckedChange={(value) =>
                    setAdd((current) => ({ ...current, keepDup: value }))
                  }
                />
                插入前不移除已有同名标签
              </label>
            </div>
            <div
              className={
                add.conditional
                  ? 'flex items-center gap-1.5 pl-[18px]'
                  : 'flex items-center gap-1.5 pl-[18px] opacity-50'
              }
            >
              <span>匹配方式</span>
              <Select
                value={add.matchMode}
                disabled={!add.conditional}
                onChange={(event) =>
                  setAdd((current) => ({ ...current, matchMode: event.target.value }))
                }
              >
                <option value="exact">精确</option>
                <option value="danbooru">Danbooru 通配(*)</option>
                <option value="regex">正则</option>
              </Select>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
              <label className="flex cursor-pointer items-center gap-1">
                <Checkbox
                  checked={add.injectPony}
                  onCheckedChange={(value) =>
                    setAdd((current) => ({ ...current, injectPony: value }))
                  }
                />
                注入美学档位
              </label>
              <label className="flex cursor-pointer items-center gap-1">
                <Checkbox
                  checked={add.injectYear}
                  onCheckedChange={(value) =>
                    setAdd((current) => ({ ...current, injectYear: value }))
                  }
                />
                注入年份标签
              </label>
            </div>
          </div>
          <div className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-[#0a1a30] px-2.5 py-2 font-mono text-xs text-muted-foreground">
            {add.result}
          </div>
        </div>
      </Dialog>

      <Dialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="批量删除标签"
        className="max-h-[70vh]"
        footer={
          <>
            <Button disabled={del.busy} onClick={() => void runBatchEdit('delete', true)}>
              预览
            </Button>
            <Button variant="danger" disabled={del.busy} onClick={() => void runBatchEdit('delete', false)}>
              写入
            </Button>
            <Button onClick={() => setDeleteOpen(false)}>关闭</Button>
          </>
        }
      >
        <div className="flex flex-col gap-2.5">
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            要删除的标签（逗号分隔）
            <Input
              placeholder="tag1, tag2"
              value={del.tags}
              onChange={(event) =>
                setDel((current) => ({ ...current, tags: event.target.value }))
              }
            />
          </label>
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-xs text-muted-foreground">
            <span>匹配方式</span>
            <Select
              value={del.matchMode}
              onChange={(event) =>
                setDel((current) => ({ ...current, matchMode: event.target.value }))
              }
            >
              <option value="exact">精确</option>
              <option value="danbooru">Danbooru 通配(*)</option>
              <option value="regex">正则</option>
            </Select>
            <span className="opacity-75">通配: * 匹配任意字符；正则作用于归一化标签(小写无下划线)</span>
          </div>
          <div className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-[#0a1a30] px-2.5 py-2 font-mono text-xs text-muted-foreground">
            {del.result}
          </div>
        </div>
      </Dialog>

      <Dialog
        open={replaceOpen}
        onOpenChange={setReplaceOpen}
        title="批量替换标签"
        className="max-h-[72vh]"
        footer={
          <>
            <Button disabled={replace.busy} onClick={() => void runBatchEdit('replace', true)}>
              预览
            </Button>
            <Button variant="danger" disabled={replace.busy} onClick={() => void runBatchEdit('replace', false)}>
              写入
            </Button>
            <Button onClick={() => setReplaceOpen(false)}>关闭</Button>
          </>
        }
      >
        <div className="flex flex-col gap-2.5">
          <div className="flex items-start gap-2.5">
            <label className="flex flex-1 flex-col gap-1 text-xs text-muted-foreground">
              原标签（逗号分隔）
              <Input
                placeholder="tag1, tag2"
                value={replace.from}
                onChange={(event) =>
                  setReplace((current) => ({ ...current, from: event.target.value }))
                }
              />
            </label>
            <label className="flex flex-1 flex-col gap-1 text-xs text-muted-foreground">
              新标签（逗号分隔，按顺序对应）
              <Input
                placeholder="new1, new2"
                value={replace.to}
                onChange={(event) =>
                  setReplace((current) => ({ ...current, to: event.target.value }))
                }
              />
            </label>
          </div>
          <div className="text-xs text-muted-foreground">
            原位置替换：原标签出现的位置改写为新标签，替换后整体去重。两个列表按顺序一一配对。
          </div>
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-xs text-muted-foreground">
            <span>原标签匹配方式</span>
            <Select
              value={replace.matchMode}
              onChange={(event) =>
                setReplace((current) => ({ ...current, matchMode: event.target.value }))
              }
            >
              <option value="exact">精确</option>
              <option value="danbooru">Danbooru 通配(*)</option>
              <option value="regex">正则</option>
            </Select>
            <span className="opacity-75">通配: * 匹配任意字符；正则作用于归一化标签(小写无下划线)</span>
          </div>
          <div className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-[#0a1a30] px-2.5 py-2 font-mono text-xs text-muted-foreground">
            {replace.result}
          </div>
        </div>
      </Dialog>

      <TaggingDialog
        open={taggingOpen}
        onClose={() => setTaggingOpen(false)}
        dirs={dirs}
        dataset={dataset}
        onDone={handleTaggingDone}
        onRunningChange={setTaggingRunning}
      />
    </div>
  );
}

/**
 * 自动生成（WD14）对话框：参数、模型状态、SSE 进度与日志。
 *
 * Args:
 *   open (boolean): 是否打开。
 *   onClose (function): 关闭回调。
 *   dirs (string[]): 当前选中的工作目录。
 *   dataset (string): 当前数据集名，用于默认触发词与触发词写入。
 *   onDone (function): 打标完成后的数据刷新回调。
 *   onRunningChange (function): 打标运行态变化回调，供工具栏同步主按钮文案与目录选择状态。
 *
 * Returns:
 *   JSX.Element: 自动生成对话框。
 *
 * Callers:
 *   - `TagsPage`
 */
function TaggingDialog({
  open,
  onClose,
  dirs,
  dataset,
  onDone,
  onRunningChange,
}: {
  open: boolean;
  onClose: () => void;
  dirs: string[];
  dataset: string;
  onDone: () => void;
  onRunningChange: (running: boolean) => void;
}) {
  const { notify, saveSettings, settings } = useApp();
  const saved = useRef<TaggingSettings>(
    settings.tagging && typeof settings.tagging === 'object'
      ? (settings.tagging as TaggingSettings)
      : {},
  );
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  const [modelReady, setModelReady] = useState(false);
  const [modelPath, setModelPath] = useState('');
  const [threshold, setThreshold] = useState(
    saved.current.threshold != null ? String(saved.current.threshold) : '',
  );
  const [artistTag, setArtistTag] = useState(saved.current.artist_tag ?? '');
  const [disableOrig, setDisableOrig] = useState(!!saved.current.disable_orig_tags);
  const [force, setForce] = useState(!!saved.current.force);
  const [snapshot, setSnapshot] = useState<JobSnapshot | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await request<TaggingDirsData>(apiPath('/tagging/dirs'));
        if (cancelled) return;
        setModelReady(data.model_ready);
        setModelPath(data.model_path);
        if (saved.current.threshold == null) {
          setThreshold(String(data.default_threshold));
        }
      } catch (error) {
        if (!cancelled) notify(messageOf(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [notify]);

  useEffect(() => {
    if (!open) return;
    setArtistTag((current) => (current.trim() ? current : dataset));
  }, [open, dataset]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const active = await api.findActiveJob('tagging');
        if (cancelled || !active?.job_id) return;
        setSnapshot(active as JobSnapshot);
        if (active.log?.length || (active as JobSnapshot).logs?.length) {
          setLogs((active as JobSnapshot).logs ?? active.log ?? []);
        }
        setJobId(active.job_id);
        notify('已恢复打标进度');
      } catch (error) {
        if (!cancelled) notify(messageOf(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [notify]);

  useEffect(() => {
    if (!jobId) return;
    return api.watchJob(jobId, (raw) => {
      const snap = raw as JobSnapshot;
      setSnapshot(snap);
      if (snap.logs && snap.logs.length) setLogs(snap.logs);
      if (snap.status === 'done') {
        notify('打标完成');
        void doneRef.current();
        setJobId(null);
      } else if (snap.status === 'cancelled') {
        notify('已取消');
        setJobId(null);
      } else if (snap.status === 'error') {
        notify('打标失败');
        setJobId(null);
      }
    });
  }, [jobId, notify]);

  const status = snapshot?.status ?? 'idle';
  const running = status === 'running' || status === 'queued';

  useEffect(() => {
    onRunningChange(running);
  }, [onRunningChange, running]);

  useEffect(() => {
    const box = logRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [logs.length, open]);

  useJob(
    snapshot
      ? {
          snapshot,
          onCancel: jobId ? () => void api.cancelJob(jobId) : undefined,
          cancelling: Boolean(snapshot.cancel_requested),
          onClear: () => setSnapshot(null),
        }
      : null,
  );

  /**
   * 提交打标任务并订阅 SSE 进度。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 提交完成。
   *
   * Callers:
   *   - `TaggingDialog` 的「开始」按钮。
   */
  const startJob = async () => {
    if (!dirs.length) {
      notify('请选择目录');
      return;
    }
    const value = Number.parseFloat(threshold);
    if (!(value >= 0 && value <= 1)) {
      notify('置信度须在 0～1');
      return;
    }
    const trigger = artistTag.trim();
    try {
      const result = await request<{ job_id: string }>(apiPath('/tagging/start'), {
        method: 'POST',
        body: JSON.stringify({
          dirs,
          threshold: value,
          disable_orig_tags: disableOrig,
          force,
          artist_tag: trigger,
        }),
      });
      saveSettings({
        tagging: {
          threshold: value,
          disable_orig_tags: disableOrig,
          force,
          artist_tag: trigger,
        },
      });
      if (trigger) {
        void request(`/api/tasks/${encodeURIComponent(dataset)}/trigger-word/add`, {
          method: 'POST',
          body: JSON.stringify({ trigger_word: trigger }),
        }).catch((error) => notify(`触发词写入失败: ${messageOf(error)}`));
      }
      setSnapshot({ status: 'queued', message: '等待执行' });
      notify('已提交');
      setJobId(result.job_id);
    } catch (error) {
      notify(messageOf(error));
    }
  };

  /**
   * 请求取消当前打标任务。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>: 取消请求发送完成。
   *
   * Callers:
   *   - `TaggingDialog` 的「取消」按钮。
   */
  const cancelJob = async () => {
    if (!jobId) return;
    try {
      await api.cancelJob(jobId);
      notify('正在取消…');
    } catch (error) {
      notify(messageOf(error));
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (!value) onClose();
      }}
      title="自动生成"
      footer={
        <>
          <Button disabled={!running} onClick={() => void cancelJob()}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={running || !modelReady}
            onClick={() => void startJob()}
          >
            开始
          </Button>
          <Button onClick={onClose}>关闭</Button>
        </>
      }
    >
      <div className="flex flex-col gap-2.5">
        <div className="flex flex-wrap items-center gap-2.5 border-b border-border pb-2.5 text-xs text-muted-foreground">
          <label className="flex items-center gap-1">
            置信度
            <Input
              className="w-16"
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={threshold}
              disabled={running}
              onChange={(event) => setThreshold(event.target.value)}
            />
          </label>
          <label className="flex items-center gap-1">
            触发词
            <Input
              className="w-32"
              placeholder="@画师名或普通标签"
              spellCheck={false}
              value={artistTag}
              onChange={(event) => setArtistTag(event.target.value)}
            />
          </label>
          <label className="flex items-center gap-1">
            <Checkbox
              checked={disableOrig}
              disabled={running}
              onCheckedChange={setDisableOrig}
            />
            忽略已有
          </label>
          <label className="flex items-center gap-1">
            <Checkbox checked={force} disabled={running} onCheckedChange={setForce} />
            覆盖
          </label>
        </div>

        <div className="flex items-center gap-2 border-b border-border pb-2 text-xs text-muted-foreground">
          <span>模型</span>
          <b className={modelReady ? 'text-foreground' : 'text-primary'}>
            {modelReady ? '就绪' : modelPath || '未就绪'}
          </b>
        </div>

        <div
          ref={logRef}
          className="max-h-[45vh] min-h-40 overflow-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-[#c8d4e0]"
        >
          {logs.join('\n')}
        </div>
      </div>
    </Dialog>
  );
}
