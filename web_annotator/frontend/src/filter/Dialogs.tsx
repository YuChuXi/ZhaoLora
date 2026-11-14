import { useEffect, useState } from 'react';
import { api, pageUrl, sampleUrl, statusLabel } from '@/lib/api';
import type { BatchParams, BatchSnapshot, DownloadEntry, LocalItem, Post } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/controls';
import { Dialog } from '@/components/ui/dialog';
import { useJob } from '@/components/ui/status';
import { isRunning, type SimilarState } from './state';

export function PreviewDialog({
  post,
  entry,
  onClose,
  onToggle,
  onTagSearch,
}: {
  post: Post | null;
  entry?: DownloadEntry;
  onClose: () => void;
  onToggle: (post: Post) => void;
  onTagSearch: (tag: string) => void;
}) {
  if (!post) return null;
  const saved = entry?.status === 'done';
  const downloading = entry?.status === 'downloading';
  const tags = (post.tags || '').split(/\s+/).filter(Boolean);
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={`${post.source} · ${post.stem || post.id}`}
      className="w-[min(960px,94vw)]"
    >
      <div className="flex max-h-[70vh] gap-4">
        <div className="flex min-w-0 flex-1 items-center justify-center overflow-hidden rounded-md bg-[#05070d]">
          <img src={sampleUrl(post)} alt={post.stem || post.id} className="max-h-[68vh] max-w-full object-contain" />
        </div>
        <div className="flex w-72 shrink-0 flex-col gap-3 text-xs text-muted-foreground">
          <div>
            <div className="text-foreground">{post.stem || post.id}</div>
            <div>分辨率 {post.width > 0 ? `${post.width}×${post.height}` : '-'}</div>
            <div>分数 {post.score ?? '-'} · rating {post.rating || '-'}</div>
            <div>状态 {statusLabel(entry?.status)}</div>
          </div>
          <div className="flex flex-wrap gap-1.5 overflow-auto">
            {tags.map((tag) => (
              <button
                key={tag}
                type="button"
                className="max-w-full break-all rounded border border-border bg-[#0a1a30] px-2 py-1 text-[11px] text-foreground hover:border-primary"
                onClick={() => onTagSearch(tag)}
              >
                {tag}
              </button>
            ))}
          </div>
          <div className="mt-auto flex flex-wrap gap-2">
            <a
              className="inline-flex h-7 items-center rounded-md border border-border px-3 text-xs text-foreground hover:bg-accent"
              href={pageUrl(post)}
              target="_blank"
              rel="noreferrer"
            >
              打开原站
            </a>
            <Button variant={saved ? 'default' : 'primary'} onClick={() => onToggle(post)}>
              {downloading ? '取消保存' : saved ? '移除' : '下载'}
            </Button>
          </div>
        </div>
      </div>
    </Dialog>
  );
}

export function SimilarDialog({
  similar,
  onClose,
  onJump,
}: {
  similar: SimilarState | null;
  onClose: () => void;
  onJump: (path: string) => void;
}) {
  if (!similar) return null;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()} title="色调相似" className="w-[min(720px,94vw)]">
      {similar.loading ? (
        <div className="py-8 text-center text-xs text-muted-foreground">加载中…</div>
      ) : (
        <div className="grid grid-cols-3 gap-2">
          {similar.items.map((item: LocalItem) => (
            <button
              key={item.path}
              type="button"
              className="overflow-hidden rounded border border-border text-left hover:border-primary"
              onClick={() => {
                onJump(item.path);
                onClose();
              }}
            >
              <img
                src={item.thumbnail_hash ? api.thumbHashUrl(item.thumbnail_hash) : api.localImageUrl(item.path)}
                alt={item.stem}
                loading="lazy"
                className="block aspect-square w-full object-cover"
              />
              <div className="px-2 py-1 text-[11px] text-muted-foreground">
                {item.width > 0 ? `${item.width}×${item.height} · ` : ''}
                {item.stem}
              </div>
            </button>
          ))}
        </div>
      )}
    </Dialog>
  );
}

export function ConditionDialog({
  open,
  extra,
  onClose,
  onApply,
}: {
  open: boolean;
  extra: string;
  onClose: () => void;
  onApply: (extra: string) => void;
}) {
  const [ratings, setRatings] = useState<string[]>([]);
  const [score, setScore] = useState('');
  const [width, setWidth] = useState('');
  const [height, setHeight] = useState('');
  const [others, setOthers] = useState('');

  useEffect(() => {
    if (!open) return;
    const tokens = extra.split(/\s+/).filter(Boolean);
    setRatings(tokens.filter((token) => token.startsWith('rating:')).map((token) => token.slice(7)));
    setScore(tokens.find((token) => token.startsWith('score:'))?.slice(6) ?? '');
    setWidth(tokens.find((token) => token.startsWith('width:'))?.slice(6) ?? '');
    setHeight(tokens.find((token) => token.startsWith('height:'))?.slice(7) ?? '');
    setOthers(
      tokens
        .filter((token) => !/^(rating|score|width|height):/.test(token))
        .join(' '),
    );
  }, [open, extra]);

  const apply = () => {
    const parts: string[] = [];
    for (const rating of ratings) parts.push(`rating:${rating}`);
    if (score.trim()) parts.push(`score:${score.trim()}`);
    if (width.trim()) parts.push(`width:${width.trim()}`);
    if (height.trim()) parts.push(`height:${height.trim()}`);
    if (others.trim()) parts.push(others.trim());
    onApply(parts.join(' '));
    onClose();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title="搜索条件"
      footer={
        <>
          <Button onClick={onClose}>关闭</Button>
          <Button variant="primary" onClick={apply}>
            应用
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3 text-xs text-muted-foreground">
        <div className="flex items-center gap-2">
          <span className="w-16">评级</span>
          {['g', 's', 'q', 'e'].map((rating) => (
            <label key={rating} className="flex items-center gap-1">
              <Checkbox
                checked={ratings.includes(rating)}
                onCheckedChange={(checked) =>
                  setRatings(
                    checked ? [...ratings, rating] : ratings.filter((item) => item !== rating),
                  )
                }
              />
              {rating}
            </label>
          ))}
        </div>
        <label className="flex items-center gap-2">
          <span className="w-16">分数</span>
          <Input
            className="flex-1"
            placeholder="如 >=100"
            value={score}
            onChange={(event) => setScore(event.target.value)}
          />
        </label>
        <label className="flex items-center gap-2">
          <span className="w-16">宽度</span>
          <Input
            className="flex-1"
            placeholder="如 >=1920"
            value={width}
            onChange={(event) => setWidth(event.target.value)}
          />
        </label>
        <label className="flex items-center gap-2">
          <span className="w-16">高度</span>
          <Input
            className="flex-1"
            placeholder="如 >=1080"
            value={height}
            onChange={(event) => setHeight(event.target.value)}
          />
        </label>
        <label className="flex items-center gap-2">
          <span className="w-16">其它</span>
          <Input
            className="flex-1"
            placeholder="额外条件，空格分隔"
            value={others}
            onChange={(event) => setOthers(event.target.value)}
          />
        </label>
      </div>
    </Dialog>
  );
}

export function SearchDialog({
  open,
  busy,
  message,
  onClose,
  onSemantic,
  onTag,
}: {
  open: boolean;
  busy: boolean;
  message: string;
  onClose: () => void;
  onSemantic: (query: string) => void;
  onTag: (query: string) => void;
}) {
  const [mode, setMode] = useState<'semantic' | 'tag'>('semantic');
  const [query, setQuery] = useState('');
  const [history, setHistory] = useState<Array<{ query: string; time?: number }>>([]);

  useEffect(() => {
    if (!open) return;
    api
      .embeddingHistory()
      .then((result) => setHistory(result.queries ?? []))
      .catch(() => setHistory([]));
  }, [open]);

  const submit = () => {
    const text = query.trim();
    if (!text || busy) return;
    if (mode === 'semantic') onSemantic(text);
    else onTag(text);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title="搜索排序"
      footer={
        <>
          <Button onClick={onClose}>关闭</Button>
          <Button variant="primary" disabled={busy} onClick={submit}>
            {busy ? '处理中…' : '按相似度排序'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2 text-xs text-muted-foreground">
        <label className="flex items-center gap-2">
          模式
          <Select value={mode} onChange={(event) => setMode(event.target.value as 'semantic' | 'tag')}>
            <option value="semantic">自然语言描述</option>
            <option value="tag">标签评分</option>
          </Select>
        </label>
        <Input
          value={query}
          placeholder={mode === 'semantic' ? '描述画面，如 白发的少女站在海边' : '标签，逗号分隔，如 1girl, solo'}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              submit();
            }
          }}
        />
        {mode === 'semantic' && history.length ? (
          <Select value="" onChange={(event) => setQuery(event.target.value)}>
            <option value="">最近搜索</option>
            {history.map((item) => (
              <option key={item.query} value={item.query}>
                {item.query}
              </option>
            ))}
          </Select>
        ) : null}
        {message ? <div className="text-[11px] text-foreground">{message}</div> : null}
      </div>
    </Dialog>
  );
}

export function BatchDialog({
  open,
  snapshot,
  onClose,
  onStart,
  onParams,
}: {
  open: boolean;
  snapshot: BatchSnapshot | null;
  onClose: () => void;
  onStart: (params: BatchParams) => void;
  onParams: (params: BatchParams) => void;
}) {
  const [params, setParams] = useState<BatchParams>({
    limit: 50,
    conn: 2,
    rate: 0,
    anima: true,
    meta: false,
    underscores: false,
  });
  const running = isRunning(snapshot);

  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => onParams(params), 300);
    return () => window.clearTimeout(timer);
  }, [params, running, onParams]);

  const jobId = snapshot?.job_id;
  useJob(
    open && snapshot
      ? {
          snapshot: { ...snapshot, total: snapshot.limit ?? null },
          onCancel: jobId ? () => void api.cancelJob(jobId) : undefined,
        }
      : null,
  );

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title="批量下载"
      footer={
        <>
          <Button onClick={onClose}>关闭</Button>
          <Button variant="primary" disabled={running} onClick={() => onStart(params)}>
            {running ? '运行中…' : '开始'}
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
        <span>数量</span>
        <Input
          type="number"
          min={1}
          value={params.limit}
          onChange={(event) => setParams({ ...params, limit: Number(event.target.value) || 1 })}
        />
        <span>连接</span>
        <Input
          type="number"
          min={1}
          max={8}
          value={params.conn}
          onChange={(event) => setParams({ ...params, conn: Number(event.target.value) || 1 })}
        />
        <span>限速</span>
        <Input
          type="number"
          min={0}
          step={0.5}
          value={params.rate}
          onChange={(event) => setParams({ ...params, rate: Number(event.target.value) || 0 })}
        />
        <span>格式</span>
        <label className="flex items-center gap-2">
          <Checkbox checked={params.anima} onCheckedChange={(checked) => setParams({ ...params, anima: checked })} />
          Anima 格式
        </label>
        <span />
        <label className="flex items-center gap-2">
          <Checkbox checked={params.meta} onCheckedChange={(checked) => setParams({ ...params, meta: checked })} />
          包含元数据标签
        </label>
        <span />
        <label className="flex items-center gap-2">
          <Checkbox
            checked={params.underscores}
            onCheckedChange={(checked) => setParams({ ...params, underscores: checked })}
          />
          保留下划线
        </label>
      </div>
    </Dialog>
  );
}

export function MetaDialog({
  open,
  snapshot,
  onClose,
  onRegenerate,
  onRefresh,
}: {
  open: boolean;
  snapshot: BatchSnapshot | null;
  onClose: () => void;
  onRegenerate: () => void;
  onRefresh: () => void;
}) {
  const running = isRunning(snapshot);
  const jobId = snapshot?.job_id;
  useJob(
    open && snapshot
      ? {
          snapshot,
          onCancel: jobId ? () => void api.cancelJob(jobId) : undefined,
        }
      : null,
  );

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title="元数据工具"
      footer={<Button onClick={onClose}>关闭</Button>}
    >
      <div className="flex flex-col gap-2 text-xs text-muted-foreground">
        <Button variant="primary" disabled={running} onClick={onRegenerate}>
          重生成 Caption
        </Button>
        <Button disabled={running} onClick={onRefresh}>
          刷新元数据
        </Button>
      </div>
    </Dialog>
  );
}
