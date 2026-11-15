import { useEffect } from 'react';
import { useApp } from '@/app/AppContext';
import { cn } from '@/lib/utils';
import { Button } from './button';

/**
 * 状态段语义。
 *
 * - `total`：当前集合规模，标签固定为"共"。
 * - `part`：集合的分类计数，必须给出分类名。
 * - `selected`：选中数量，标签固定为"已选"。
 * - `resource`：外部资源状态，必须给出资源名。
 * - `nav`：页面导航入口（如当前位置、缩略条开关），必须给出名称与点击行为。
 */
export type StatusKind = 'total' | 'part' | 'selected' | 'resource' | 'nav';

export type StatusSegData =
  | { kind: 'total'; value: string | number }
  | { kind: 'part'; name: string; value: string | number }
  | { kind: 'selected'; value: string | number }
  | { kind: 'resource'; name: string; value: string | number }
  | { kind: 'nav'; name: string; value: string | number; onClick: () => void; active?: boolean };

/** 长任务快照的最小结构，与后端 `services/models/jobs.py:JobSnapshot` 字段对齐。 */
export interface JobSnapshotLike {
  status?: string;
  message?: string;
  done?: number | null;
  total?: number | null;
  failed?: number | null;
  skipped?: number | null;
  pct?: number | null;
  cancel_requested?: boolean;
  current?: string;
}

/** 状态栏左端展示的任务，含取消与清除入口。 */
export interface JobState {
  snapshot: JobSnapshotLike;
  onCancel?: () => void;
  canCancel?: boolean;
  cancelling?: boolean;
  onClear?: () => void;
}

const KIND_ORDER: Record<StatusKind, number> = {
  total: 0,
  nav: 1,
  part: 2,
  selected: 3,
  resource: 4,
};

const STATUS_TEXT: Record<string, string> = {
  idle: '空闲',
  queued: '排队',
  running: '运行中',
  done: '完成',
  error: '失败',
  cancelled: '已取消',
};

/**
 * 状态段标签：固定语义的段由种类决定，分类段使用页面给出的分类名。
 *
 * Args:
 *   seg (StatusSegData): 统计分段。
 *
 * Returns:
 *   string: 段标签。
 *
 * Callers:
 *   - `components/ui/status.tsx:StatusBar`
 */
function statusLabelOf(seg: StatusSegData): string {
  if (seg.kind === 'total') return '共';
  if (seg.kind === 'selected') return '已选';
  return seg.name;
}

/**
 * 状态栏中的一段：标签 + 强调数值。
 *
 * Args:
 *   label (string): 段标签。
 *   value (string | number): 段数值。
 *   onClick (function | undefined): 导航段的点击行为；缺省渲染为静态文本。
 *   active (boolean | undefined): 导航段对应的面板是否展开。
 *
 * Returns:
 *   JSX.Element: 状态段节点。
 *
 * Callers:
 *   - `components/ui/status.tsx:StatusBar`
 */
function StatusSeg({
  label,
  value,
  onClick,
  active,
}: {
  label: string;
  value: string | number;
  onClick?: () => void;
  active?: boolean;
}) {
  const content = (
    <>
      {label} <b className="text-foreground">{value}</b>
    </>
  );
  if (!onClick) return <span className="whitespace-nowrap">{content}</span>;
  return (
    <button
      type="button"
      className={cn(
        'whitespace-nowrap rounded px-1.5 py-0.5 hover:bg-white/10',
        active && 'bg-white/10 text-foreground',
      )}
      onClick={onClick}
    >
      {content}
    </button>
  );
}

/**
 * 全站唯一状态栏：固定在窗口底部，横跨整个应用，不随内容滚动。
 *
 * 左侧为当前任务（状态、进度、计数、取消/清除），右侧为页面统计与摘要。
 * 页面与对话框只上报数据，不渲染自己的状态条。
 *
 * Args:
 *   segs (StatusSegData[]): 统计分段。
 *   summary (string): 摘要文本。
 *   job (JobState | null): 当前任务。
 *
 * Returns:
 *   JSX.Element: 状态栏节点。
 *
 * Callers:
 *   - `app/AppShell.tsx:AppShell`
 */
export function StatusBar({
  segs,
  summary,
  job,
}: {
  segs: StatusSegData[];
  summary: string;
  job: JobState | null;
}) {
  const visible = segs.slice().sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind]);
  const snapshot = job?.snapshot ?? null;
  const status = snapshot?.status ?? '';
  const active = status === 'running' || status === 'queued';
  const queued = status === 'queued';
  const done = snapshot?.done ?? 0;
  const total = snapshot?.total ?? 0;
  const derived = total > 0 ? (done / total) * 100 : 0;
  const pct = Math.max(0, Math.min(100, snapshot?.pct ?? derived));
  const counts: string[] = [];
  if (snapshot && total > 0) counts.push(`${done}/${total}`);
  if (snapshot?.skipped) counts.push(`跳过 ${snapshot.skipped}`);
  if (snapshot?.failed) counts.push(`失败 ${snapshot.failed}`);

  return (
    <div className="flex h-7 shrink-0 items-center gap-2 border-t border-border bg-[#0f1729] px-3 text-xs text-muted-foreground">
      {snapshot ? (
        <>
          {active ? (
            <div className="h-1 w-24 shrink-0 overflow-hidden rounded bg-border">
              <div
                className={
                  queued
                    ? 'h-full w-1/3 animate-pulse bg-primary'
                    : 'h-full bg-primary transition-all'
                }
                style={queued ? undefined : { width: `${pct}%` }}
              />
            </div>
          ) : null}
          <span className="whitespace-nowrap">
            {snapshot.cancel_requested && status === 'running'
              ? '取消中'
              : STATUS_TEXT[status] ?? status}
            {active && !queued ? ` ${Math.round(pct)}%` : ''}
          </span>
          {snapshot.message ? (
            <span className="max-w-[36vw] truncate">{snapshot.message}</span>
          ) : null}
          {snapshot.current ? (
            <span className="max-w-[24vw] truncate">{snapshot.current}</span>
          ) : null}
          {counts.length ? <span className="whitespace-nowrap">{counts.join(' · ')}</span> : null}
          {active && job?.onCancel && (job.canCancel ?? true) ? (
            <Button size="sm" variant="danger" disabled={job.cancelling} onClick={job.onCancel}>
              取消
            </Button>
          ) : null}
          {!active && job?.onClear ? (
            <Button size="sm" variant="ghost" onClick={job.onClear} title="清除任务结果">
              ✕
            </Button>
          ) : null}
        </>
      ) : (
        <span>就绪</span>
      )}

      <span className="ml-auto flex items-center gap-3">
        {visible.map((seg, index) => (
          <StatusSeg
            key={`${seg.kind}-${index}`}
            label={statusLabelOf(seg)}
            value={seg.value}
            onClick={seg.kind === 'nav' ? seg.onClick : undefined}
            active={seg.kind === 'nav' ? seg.active : undefined}
          />
        ))}
        {summary ? <span className="max-w-[32vw] truncate">{summary}</span> : null}
      </span>
    </div>
  );
}

/**
 * 上报页面统计分段与摘要到全站状态栏。
 *
 * 每次渲染都会重新上报，由 Shell 按内容去重；组件卸载时清空，避免残留上一个页面的数据。
 *
 * Args:
 *   segs (StatusSegData[]): 统计分段。
 *   summary (string | undefined): 摘要文本。
 *
 * Returns:
 *   void
 *
 * Callers:
 *   - 各业务页。
 */
export function useStatus(segs: StatusSegData[], summary?: string): void {
  const { setStatus } = useApp();
  useEffect(() => {
    setStatus(segs, summary ?? '');
  });
  useEffect(() => () => setStatus([], ''), [setStatus]);
}

/**
 * 上报当前长任务到全站状态栏。
 *
 * 页面与对话框均可调用；同一时刻只有一个任务占据状态栏左端。快照为 null 时清空。
 *
 * Args:
 *   state (JobState | null): 任务快照与操作入口。
 *
 * Returns:
 *   void
 *
 * Callers:
 *   - 提交长任务的页面与对话框。
 */
export function useJob(state: JobState | null): void {
  const { setJob } = useApp();
  useEffect(() => {
    setJob(state);
  });
  useEffect(() => () => setJob(null), [setJob]);
}
