import { useEffect, useRef, useState } from 'react';
import { api, apiPath, request } from '@/lib/api';
import type { BatchSnapshot } from '@/lib/types';
import type { JobSnapshotLike } from '@/components/ui/status';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/controls';
import { Dialog } from '@/components/ui/dialog';
import { useJob } from '@/components/ui/status';
import {
  errorMessage,
  metricOf,
  type BatchTagResponse,
  type EmbOptions,
  type Engine,
  type InsertPosition,
  type JobSnapshot,
  type SemanticItem,
  type TagCheckItem,
} from './model';

interface SemanticDialogProps {
  open: boolean;
  onClose: () => void;
  dirs: string[];
  engine: Engine;
  options: EmbOptions;
  onEngineChange: (engine: Engine) => void;
  onOptionsChange: (patch: Partial<EmbOptions>) => void;
  positionText: string;
  insertPosition: InsertPosition;
  items: TagCheckItem[];
  sortActive: boolean;
  onSorted: (engine: Engine, hits: SemanticItem[]) => void;
  onBatchDone: () => Promise<void>;
  notify: (message: string) => void;
}

/**
 * 语义检索对话框：相似度重排序与概念分割排序，并按阈值批量添加或删除标签。
 *
 * Args:
 *   open (boolean): 对话框是否可见。
 *   onClose (function): 关闭回调。
 *   dirs (string[]): 当前目录选择。
 *   engine (Engine): 当前语义检索引擎。
 *   options (EmbOptions): 持久化的批量操作参数。
 *   onEngineChange (function): 引擎切换回调。
 *   onOptionsChange (function): 批量参数变更回调。
 *   positionText (string): 工具栏位置的展示文本。
 *   insertPosition (InsertPosition): 批量添加标签时的插入位置。
 *   items (TagCheckItem[]): 当前检查结果，含排序指标。
 *   sortActive (boolean): 是否已执行过语义排序。
 *   onSorted (function): 排序完成回调，用于写回指标与顺序。
 *   onBatchDone (function): 批量写入完成回调，用于刷新标签状态。
 *   notify (function): 全局提示回调。
 *
 * Returns:
 *   JSX.Element: 语义检索对话框。
 *
 * Callers:
 *   - `pages/tag-check/TagCheckPage.tsx:TagCheckPage`
 */
export function SemanticDialog({
  open,
  onClose,
  dirs,
  engine,
  options,
  onEngineChange,
  onOptionsChange,
  positionText,
  insertPosition,
  items,
  sortActive,
  onSorted,
  onBatchDone,
  notify,
}: SemanticDialogProps) {
  const [query, setQuery] = useState('');
  const [tag, setTag] = useState('');
  const [job, setJob] = useState<JobSnapshotLike | null>(null);
  const watcher = useRef<(() => void) | null>(null);
  const queryRef = useRef<HTMLInputElement>(null);
  const busy = job?.status === 'running' || job?.status === 'queued';
  useJob(job ? { snapshot: job, onClear: () => setJob(null) } : null);

  useEffect(
    () => () => {
      watcher.current?.();
    },
    [],
  );

  useEffect(() => {
    if (!open) return;
    if (engine === 'concept' && !tag.trim() && query.trim()) setTag(query.trim());
    queryRef.current?.focus();
  }, [open]);

  /**
   * 提交语义查询长任务并订阅进度，完成时写回排序指标。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>
   *
   * Callers:
   *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog` 排序按钮与查询框 Enter
   */
  const sort = async () => {
    const text = query.trim();
    if (!text) {
      notify('请输入查询文本');
      return;
    }
    const endpoint = engine === 'concept' ? '/tags/concept/query' : '/tags/embedding/query';
    watcher.current?.();
    setJob({ status: 'queued', message: '提交中…' });
    try {
      const result = await request<{ job_id?: string }>(apiPath(endpoint), {
        method: 'POST',
        body: JSON.stringify({ query: text, dirs }),
      });
      if (!result.job_id) {
        setJob({ status: 'error', message: '查询失败' });
        return;
      }
      watcher.current = api.watchJob(result.job_id, (snapshot: BatchSnapshot) => {
        const state = snapshot as JobSnapshot;
        setJob(state);
        if (state.status === 'done') {
          const hits = state.result?.items ?? [];
          if (!hits.length) {
            setJob({ status: 'done', message: '无匹配结果' });
            return;
          }
          onSorted(engine, hits);
          const message =
            engine === 'concept'
              ? `已按掩码占比排序 (${hits.length} 条)`
              : `已按相似度排序 (${hits.length} 条)`;
          setJob({ status: 'done', message });
          notify(message);
        } else if (state.status === 'error') {
          setJob({ status: 'error', message: state.message || '查询失败' });
          notify(`查询失败: ${state.message ?? ''}`);
        } else if (state.status === 'cancelled') {
          setJob({ status: 'cancelled', message: '已取消' });
        }
      });
    } catch (error) {
      setJob({ status: 'error', message: '查询失败' });
      notify(`查询失败: ${errorMessage(error)}`);
    }
  };

  /**
   * 按当前引擎指标与阈值筛选图片并批量添加或删除标签。
   *
   * Args:
   *   无。
   *
   * Returns:
   *   Promise<void>
   *
   * Callers:
   *   - `pages/tag-check/SemanticDialog.tsx:SemanticDialog` 批量按钮与标签框 Enter
   */
  const batchTag = async () => {
    const metricName = engine === 'concept' ? '掩码占比' : '相似度';
    const text = tag.trim() || (engine === 'concept' ? query.trim() : '');
    if (!text) {
      notify('请输入标签');
      return;
    }
    if (!sortActive) {
      notify(`请先按${metricName}排序`);
      return;
    }
    const threshold = Number(options.threshold);
    if (!options.threshold.trim() || !Number.isFinite(threshold) || threshold < 0 || threshold > 1) {
      notify('阈值必须在 0~1 之间');
      return;
    }
    const paths: string[] = [];
    for (const item of items) {
      const value = metricOf(item, engine);
      if (value == null) continue;
      if (options.operator === 'gt' ? value >= threshold : value <= threshold) {
        paths.push(item.path);
      }
    }
    if (!paths.length) {
      notify('无匹配图片');
      return;
    }
    setJob({ status: 'running', message: '执行中…' });
    try {
      const result = await request<BatchTagResponse>(apiPath('/tags/embedding/batch-tag'), {
        method: 'POST',
        body: JSON.stringify({
          paths,
          tag: text,
          action: options.action,
          position: insertPosition,
          fuzzy: options.fuzzy,
        }),
      });
      setJob({ status: 'done', message: result.message || '完成' });
      notify(result.message || '批量执行完成');
      await onBatchDone();
    } catch (error) {
      setJob({ status: 'error', message: '执行失败' });
      notify(`批量执行失败: ${errorMessage(error)}`);
    }
  };

  const concept = engine === 'concept';

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title="语义检索"
      className="w-[min(480px,92vw)]"
      footer={
        <>
          <Button variant="primary" disabled={busy} onClick={() => void batchTag()}>
            {options.action === 'delete' ? '批量删除' : '批量添加'}
          </Button>
          <Button onClick={onClose}>关闭</Button>
        </>
      }
    >
      <div className="flex flex-col gap-3 text-xs text-muted-foreground">
        <label className="flex items-center gap-2">
          <span className="shrink-0 text-[13px]">引擎</span>
          <Select
            className="flex-1"
            value={engine}
            onChange={(event) => {
              const next = event.target.value as Engine;
              onEngineChange(next);
              if (next === 'concept' && !tag.trim() && query.trim()) setTag(query.trim());
            }}
          >
            <option value="similarity">相似度 · Qwen3-VL-Reranker-2B</option>
            <option value="concept">概念分割 · SAM3</option>
          </Select>
        </label>

        <label className="flex items-center gap-2">
          <span className="shrink-0 text-[13px]">{concept ? '概念提示词' : '查询文本'}</span>
          <Input
            ref={queryRef}
            type="search"
            className="flex-1"
            spellCheck={false}
            autoComplete="off"
            placeholder={concept ? '概念提示词, 如 cat' : '查询文本'}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return;
              event.preventDefault();
              void sort();
            }}
          />
        </label>

        <div>
          <Button disabled={busy} onClick={() => void sort()}>
            {concept ? '按掩码占比排序' : '按相似度排序'}
          </Button>
        </div>

        <hr className="border-border" />

        <label className="flex items-center gap-2">
          <span className="shrink-0 text-[13px]">标签</span>
          <Input
            className="flex-1"
            spellCheck={false}
            placeholder="要添加或删除的标签"
            value={tag}
            onChange={(event) => setTag(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return;
              event.preventDefault();
              void batchTag();
            }}
          />
        </label>

        <div className="flex items-center gap-2">
          <span className="shrink-0 text-[13px]">位置</span>
          <span className="text-xs text-foreground">{positionText}</span>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Select
            value={options.action}
            onChange={(event) =>
              onOptionsChange({ action: event.target.value === 'delete' ? 'delete' : 'add' })
            }
          >
            <option value="add">添加标签</option>
            <option value="delete">删除标签</option>
          </Select>
          <span>{concept ? '掩码占比' : '相似度'}</span>
          <Select
            value={options.operator}
            onChange={(event) =>
              onOptionsChange({ operator: event.target.value === 'lt' ? 'lt' : 'gt' })
            }
          >
            <option value="gt">&gt;=</option>
            <option value="lt">&lt;=</option>
          </Select>
          <Input
            type="number"
            className="w-20 text-center"
            min={0}
            max={1}
            step={0.05}
            value={options.threshold}
            onChange={(event) => onOptionsChange({ threshold: event.target.value })}
          />
          {options.action === 'delete' ? (
            <label className="flex items-center gap-1.5 whitespace-nowrap">
              <Checkbox checked={options.fuzzy} onCheckedChange={(checked) => onOptionsChange({ fuzzy: checked })} />
              模糊匹配
            </label>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}
